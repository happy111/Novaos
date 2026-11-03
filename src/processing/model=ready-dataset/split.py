"""Split + pre-split feature engineering — stage 3 of the model-ready-dataset machine.

The second SageMaker Processing job of AR-18-b-2. Runs as `CohortLabelExecution`, reads
the joined cohort stage 2 wrote, engineers the stateless pre-split columns, balances the
dataset, and partitions it into train / validation / holdout.

    in:  --compiled-intake-s3-uri   the contract stage 1 wrote
         --run-id                   the run id the state machine minted
         cohort_with_features.parquet under jobs.pit_join.output_s3_uri
    out: train/validation/holdout.parquet + split_report.json under
         jobs.split.output_s3_uri

THE ORDER IS FEATURE ENGINEERING, THEN SAMPLING, THEN SPLIT, and every part of it is
pre-split and pre-validation:

  1. FEATURE ENGINEERING — the form's ordered `feature_engineering` steps (§5), each
     writing the one column its `name` declares.
  2. SAMPLING — `dataset_assembly.sampling`, immediately before the split, so a
     sampling function can reference a column step 1 wrote.
  3. SPLIT — `splits.strategy`, returning the three partitions.

SPLITTING RUNS STRICTLY BEFORE VALIDATION (AC-5). A dataset can pass every aggregate
check and still produce a degenerate partition, so only the post-split artifacts are
worth gating. This stage therefore does not reject an empty or single-class partition:
stage 4 checks each split independently (AC-6) and persists the report AC-8 requires,
and failing here would lose that artifact.

NOTHING IS FITTED IN THIS STAGE. The user's brief asks that pre-split feature
engineering and any preprocessing be fitted on train only; in this pipeline that has no
work to do, and deliberately so. The MRD is shared, so every model training against it
reuses the same rows, which means a step here must be stateless — its output for a row
cannot depend on which partition the other rows land in. The template's §5 says so
outright and redirects anything that fits a parameter from the data to
`model-training-config.yaml` §3 (AR-19), which runs after the split and fits on the
train partition alone; stage 1 enforces it by ERRORing on `sklearn.*` here. The current
template carries no `preprocessing` section at all, so nothing in a validated form can
ask this stage to fit anything. `ar_transformations.preprocessing` exists but has no
configuration surface in this form — reported, not worked around.

The YAML intake form is never re-parsed: the compiled intake is the contract between
the stages and nothing here re-derives what stage 1 decided (SPEC-M4 §2.3). The
windowing columns the cohort carries (`anchor_date`, `index_date`, the window bounds,
`mask_months`, `label_mature`, `censored`, `sample_weight`) are passed through
untouched — two implementations of one windowing rule are free to drift.

Nothing is read through a Processing channel. Every object is fetched with boto3 so that
`ExpectedBucketOwner` is on every call, which a `ProcessingInput` download cannot be
(handoff §6.5).
"""

import argparse
import io
import json
import os
import sys

import boto3
import pandas as pd

from src.common import intake_schema
from src.common.mrd_compiler import utc_now

# config.py and engine_dispatch.py sit beside this script. The stage directory is
# hyphenated and so is not an importable package (SPEC-M4 §6.6), which is why they
# are reached by path rather than by `from src.processing...`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as job_config  # noqa: E402 — needs the path insert above
import engine_dispatch  # noqa: E402 — same
import s3_io  # noqa: E402 — same

# Stage 2's fixed output object name. Read by name rather than by listing the prefix:
# the same stage writes its report there, and only one of the two is the dataset.
INPUT_OBJECT = "cohort_with_features.parquet"

# A fixed name, not timestamped: the output prefix is [Run ID]/splits/, already unique
# per run.
REPORT_OBJECT = "split_report.json"

# The three partitions, in the order the report lists them. A strategy returning any
# other set of keys is refused rather than having the extras silently dropped.
SPLIT_NAMES = ("train", "validation", "holdout")

# Top-level keys this job reads. A missing one means the document was written by an
# incompatible stage 1, not something to recompute from the form.
REQUIRED_KEYS = (
    "run_id",
    "env",
    "model_name",
    "upstream",
    "dataset_assembly",
    "feature_engineering",
    "splits",
    "jobs",
)

# `feature_engineering[].dtype`, which the template makes REQUIRED, mapped to the
# pandas dtype the output column is stored as. All four are NULLABLE: a transformation
# may legitimately return nulls (the `ratio` built-in yields null for a null
# denominator), and a non-nullable cast would fail on them rather than carry them.
DTYPES = {
    "float": "Float64",
    "integer": "Int64",
    "string": "string",
    "boolean": "boolean",
}


class SplitError(Exception):
    """The job cannot run: a malformed argument, an unusable compiled intake, or a
    strategy that returned something this stage cannot partition."""


# The S3 helpers and the structured logger live in `s3_io.py`, shared with the other
# two stages of this pipeline.
_log = s3_io.log


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Feature engineering + sampling + split (stage 3 of the "
        "model-ready-dataset pipeline)."
    )
    parser.add_argument("--compiled-intake-s3-uri", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    # Reuses the delivered helper so both pipelines agree on the format rather than
    # carrying a second definition of it.
    if not intake_schema.is_valid_run_id(args.run_id):
        raise SplitError(
            f"run_id: {args.run_id!r} must be 'r' followed by 12 lowercase base32 "
            "characters, e.g. r5f3a9c2d1e0b."
        )
    return args


def parse_intake(text):
    """Load the compiled intake and check the keys this job reads."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise SplitError(f"compiled intake is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SplitError("compiled intake must be a JSON object.")
    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        raise SplitError("compiled intake is missing key(s): " + ", ".join(missing))
    return document


def read_joined_cohort(s3, intake, config, run_id):
    """The dataset stage 2 wrote, from `jobs.pit_join.output_s3_uri`.

    The prefix is resolved at compile time, before the first job runs, so this is a
    field of the document rather than something read out of stage 2's job description.
    """
    uri = intake["jobs"]["pit_join"]["output_s3_uri"].rstrip("/") + "/" + INPUT_OBJECT
    body = s3_io.read_bytes(s3, uri, "jobs.pit_join.output_s3_uri", config.account_id)
    frame = pd.read_parquet(io.BytesIO(body))
    _log(
        "joined cohort read",
        runId=run_id,
        inputS3Uri=uri,
        rows=len(frame),
        columns=list(frame.columns),
    )
    return frame


def cast_column(column, dtype, field):
    """Store a step's output under the dtype it declared.

    `dtype` is REQUIRED on every step, so it is honoured rather than carried and
    ignored: the parquet this stage writes is where the training pipeline reads the
    dataset's schema from.
    """
    pandas_dtype = DTYPES.get(str(dtype or "").strip().lower())
    if not pandas_dtype:
        raise SplitError(
            f"{field}.dtype is {dtype!r}, which is not one of {sorted(DTYPES)}."
        )
    try:
        return column.astype(pandas_dtype)
    except (TypeError, ValueError) as exc:
        raise SplitError(
            f"{field} returned values that do not fit its declared dtype "
            f"{dtype!r}: {exc}"
        ) from exc


def apply_feature_engineering(frame, steps, run_id):
    """Run §5's ordered steps, each writing the one column its `name` declares.

    Steps run in declaration order and each sees the columns the earlier ones wrote,
    which is what lets one step build on another. A step whose `name` is a column the
    dataset already carries overwrites it: the template lists type casts and clipping
    among the legitimate steps here, and those write back to the column they read.

    Every step is stateless by validation, so running them before the split is safe and
    "fitted on train only" has nothing to fit — see this module's docstring.

    Returns `(frame, report_entries)`.
    """
    applied = []
    for index, step in enumerate(steps):
        field = f"feature_engineering[{index}]"
        name = step.get("name")
        if not name:
            raise SplitError(
                f"{field}.name is blank, so the step has no output column."
            )
        block = step.get("transformation")
        result = engine_dispatch.call_source_columns(block, field, frame)
        # Taken positionally: a transformation returns its own frame, whose index need
        # not match the dataset's, and assigning a Series with a foreign index would
        # align on that index and silently produce nulls.
        values = pd.Series(result.iloc[:, 0].to_numpy(), index=frame.index)
        frame[name] = cast_column(values, step.get("dtype"), field)
        applied.append(
            {
                "name": name,
                "dtype": step.get("dtype"),
                "transformation": engine_dispatch.describe(block),
                "source_columns": dict(block.get("source_columns") or {}),
            }
        )
        _log(
            "feature engineered",
            runId=run_id,
            field=field,
            column=name,
            dtype=step.get("dtype"),
            transformation=engine_dispatch.describe(block),
        )
    return frame, applied


def apply_sampling(frame, block, run_id):
    """`dataset_assembly.sampling`, immediately before the split.

    Balancing is a property of the DATASET rather than of the cohort, which is why it is
    declared on this form and not upstream. It runs in this stage rather than the
    PIT-join one so that a sampling function can reference a column feature engineering
    just wrote.

    Resampling before a split can put a duplicate of a training row into holdout, which
    would make holdout metrics optimistic. The two implemented modes cannot do it —
    `undersample` only drops rows and `class_weights` resamples nothing, adding a weight
    column instead — and `oversample` / `smote` raise NotImplementedError in the
    package, so no form that runs today reaches that leak.

    Returns `(frame, report_entry_or_None)`.
    """
    if not block:
        return frame, None

    field = "dataset_assembly.sampling"
    before = len(frame)
    result = engine_dispatch.call(block, field, frame)
    if not isinstance(result, pd.DataFrame):
        raise SplitError(
            f"{field} ({engine_dispatch.describe(block)}) returned "
            f"{type(result).__name__}; a sampling strategy returns the sampled "
            f"DataFrame."
        )
    added = [column for column in result.columns if column not in frame.columns]
    entry = {
        "strategy": engine_dispatch.describe(block),
        "rows_in": before,
        "rows_out": int(len(result)),
        "columns_added": added,
    }
    _log("sampling applied", runId=run_id, field=field, **entry)
    # The index is reset because a strategy that dropped rows returns the surviving
    # ones under their original labels, and the split that follows draws positionally.
    return result.reset_index(drop=True), entry


def apply_split(frame, block, run_id):
    """`splits.strategy` — the three partitions (AC-5).

    The strategy returns a copy of `frame` with a `split` column valued
    train | validation | holdout — every strategy in the installed package
    (`time_based`, `random_by_entity`) follows this contract, not a
    `{"train": df, "validation": df, "holdout": df}` dict.

    A degenerate partition is NOT rejected here. Stage 4 gates each split independently
    and persists the validation report; refusing here would fail the run without it.
    """
    if not block:
        raise SplitError(
            "splits.strategy is null in the compiled intake, so there is nothing to "
            "dispatch. It is REQUIRED (template §6) and stage 1 ERRORs on a blank one, "
            "so this only fires for a document written by an incompatible stage 1. The "
            "stage after this one validates train, validation and holdout "
            "independently (AC-6), so an unsplit dataset has no contract to validate."
        )

    field = "splits.strategy"
    result = engine_dispatch.call(block, field, frame)
    if not isinstance(result, pd.DataFrame):
        raise SplitError(
            f"{field} ({engine_dispatch.describe(block)}) returned "
            f"{type(result).__name__}; a split strategy returns a DataFrame with a "
            f"'split' column."
        )
    if "split" not in result.columns:
        raise SplitError(
            f"{field} ({engine_dispatch.describe(block)}) returned no 'split' column. "
            f"Columns: {sorted(result.columns)}."
        )
    unexpected = sorted(set(result["split"].unique()) - set(SPLIT_NAMES))
    if unexpected:
        raise SplitError(
            f"{field} ({engine_dispatch.describe(block)}) returned unexpected split "
            f"value(s) {unexpected}. Only {list(SPLIT_NAMES)} are written, so those "
            f"rows would be dropped without a trace."
        )

    splits = {
        name: result[result["split"] == name]
        .drop(columns="split")
        .reset_index(drop=True)
        for name in SPLIT_NAMES
    }
    _log(
        "split applied",
        runId=run_id,
        field=field,
        strategy=engine_dispatch.describe(block),
        rowsIn=len(frame),
        rows={name: len(part) for name, part in splits.items()},
    )
    return splits


def build_report(intake, splits, columns, engineered, sampling, generated_at):
    """The split report — this stage's audit artifact.

    Row counts per partition are the interesting part: they are the input to stage 4's
    gate, and a partition this stage reports as empty is one stage 4 must reject rather
    than ship.
    """
    return {
        "run_id": intake["run_id"],
        "model_name": intake["model_name"],
        "generated_at": generated_at,
        "upstream_cohort_label_run_id": intake["upstream"].get("cohort_label_run_id"),
        "columns": columns,
        "feature_engineering": engineered,
        "sampling": sampling,
        "split": {
            "strategy": engine_dispatch.describe(intake["splits"]["strategy"]),
            "rows": {name: int(len(part)) for name, part in splits.items()},
        },
    }


def main(argv=None):
    args = _parse_args(argv)
    config = job_config.load()
    run_id = args.run_id

    s3 = boto3.client("s3", region_name=config.region)

    intake = parse_intake(
        s3_io.read_bytes(
            s3, args.compiled_intake_s3_uri, "compiled_intake_s3_uri", config.account_id
        ).decode("utf-8")
    )
    # The run id is the first segment of the object key, so a mismatch means this run
    # was handed another run's document.
    if intake["run_id"] != run_id:
        raise SplitError(
            f"run_id: argument {run_id!r} does not match the compiled intake's "
            f"{intake['run_id']!r}."
        )
    if intake["env"] != config.env:
        raise SplitError(
            f"env: the compiled intake was compiled for {intake['env']!r} but this "
            f"job runs in {config.env!r}."
        )

    frame = read_joined_cohort(s3, intake, config, run_id)

    frame, engineered = apply_feature_engineering(
        frame, intake["feature_engineering"], run_id
    )
    frame, sampling = apply_sampling(
        frame, intake["dataset_assembly"].get("sampling"), run_id
    )
    splits = apply_split(frame, intake["splits"]["strategy"], run_id)

    columns = list(frame.columns)
    generated_at = utc_now()
    report = build_report(intake, splits, columns, engineered, sampling, generated_at)

    output_prefix = intake["jobs"]["split"]["output_s3_uri"].rstrip("/") + "/"
    written = []
    for name in SPLIT_NAMES:
        buffer = io.BytesIO()
        splits[name].to_parquet(buffer, index=False)
        written.append(
            s3_io.put_object(
                s3,
                f"{output_prefix}{name}.parquet",
                buffer.getvalue(),
                "application/octet-stream",
                "jobs.split.output_s3_uri",
                config,
            )
        )
    written.append(
        s3_io.put_object(
            s3,
            output_prefix + REPORT_OBJECT,
            json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
            "application/json",
            "jobs.split.output_s3_uri",
            config,
        )
    )

    _log(
        "split complete",
        runId=run_id,
        rows=report["split"]["rows"],
        written=written,
    )
    return report


if __name__ == "__main__":
    main()
