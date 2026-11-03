"""Validate + write — stage 4 of the model-ready-dataset machine.

The last SageMaker Processing job of AR-18-b-2. Runs as `CohortLabelExecution`, gates
the three partitions stage 3 wrote, and — only if every check passes — writes the
versioned dataset.

    in:  --compiled-intake-s3-uri   the contract stage 1 wrote
         --run-id                   the run id the state machine minted
         train/validation/holdout.parquet under jobs.split.output_s3_uri
    out: always  -> validation.report_s3_uri
         on pass -> storage.dataset_s3_uri, then storage.dataset_latest_s3_uri
         on fail -> SNS to validation.sns_topic_arn, and NOTHING in the dataset area

EVERY CHECK RUNS AGAINST EACH SPLIT INDEPENDENTLY, never against a concatenation
(AC-6). That is the whole reason the split runs first: a dataset can pass every
aggregate check and still shatter into a degenerate partition, and only the artifacts
the model will actually train on are worth gating. Note the consequence for an author —
`min_population_size: 100` means 100 rows in the HOLDOUT too, so with a 0.15 holdout
ratio the cohort needs roughly 700 rows. The report and the alert name the split for
exactly that reason.

Four checks. Three are platform-applied because they are fields on the form rather than
dispatch blocks; the fourth is dispatched like every other strategy:

  1. `min_population_size`        — the partition's row count
  2. `max_null_rate_label`        — a FRACTION, 0.0-1.0, per label column
  3. `require_full_observability` — the cohort's own `label_mature` column. Maturity is
     pipeline 1's guard and is READ here, never recomputed: two implementations of one
     windowing rule are free to drift (SPEC-M4 §2.3)
  4. `validation.strategy`        — the type-appropriate gate from
     `ar_transformations.validation`. Consumed, not written: a missing one is an
     AR-18-b-1 gap to report (SPEC-M4 §7)

THE GATE'S EXCEPTION IS THE EXPECTED SIGNAL, NOT A CRASH. Unlike sampling and splits,
`validation.strategy` returns nothing — it RAISES when the dataset fails. Letting that
propagate would kill the job before anything was written, and AC-8 requires the report
to be persisted on failure as well as on success: the artifact, not just the message.
So every check is caught as a finding, the report is built from all of them, and only
then is the run allowed to fail.

A REJECTION IS NOT A JOB FAILURE. A failing check means the DATASET is bad: the report
is written, the alert is sent, and the run ends non-zero with the dataset area
untouched. `engine_dispatch` raising `SystemExit` — an unimportable module, a missing
function, `params` that do not bind — means THIS STAGE COULD NOT JUDGE the dataset at
all, which is a stale image or a malformed document rather than bad data. `SystemExit`
is a BaseException and so passes straight through the `except Exception` below, which is
what keeps the two apart without a second code path.

The ordering is load-bearing. The report is written BEFORE the dataset, and the
versioned copy BEFORE `latest/`: AC-7 is "nothing lands in the dataset area", not
"nothing is left there afterwards", and `latest/` must never point at a dataset that
does not exist.

The YAML intake form is never re-parsed: the compiled intake is the contract between
the stages and nothing here re-derives what stage 1 decided (SPEC-M4 §2.3). Neither is
stage 3's `split_report.json` — the row counts a gate compares against are taken from
the parquet files this job actually reads, so a wrong report cannot make the gate wrong
in the same direction.

The final dataset write stays S3-only (AC-9), whatever any interim stage's destination
was. Nothing is read or written through a Processing channel; every object goes through
boto3 so `ExpectedBucketOwner` is on every call (handoff §6.5).
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

# config.py, engine_dispatch.py and s3_io.py sit beside this script. The stage directory
# is hyphenated and so is not an importable package (SPEC-M4 §6.6), which is why they are
# reached by path rather than by `from src.processing...`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as job_config  # noqa: E402 — needs the path insert above
import engine_dispatch  # noqa: E402 — same
import s3_io  # noqa: E402 — same

# Stage 3's fixed output object names. Read by name rather than by listing the prefix:
# `split_report.json` sits beside them and is deliberately not read.
SPLIT_NAMES = ("train", "validation", "holdout")

# Written beside the three partitions, in both the versioned directory and `latest/`.
MANIFEST_OBJECT = "manifest.json"

# The maturity flag the cohort carries out of pipeline 1, and what
# `require_full_observability` reads.
MATURITY_COLUMN = "label_mature"

# The dispatched gate's field name. One definition because it is both the field
# `engine_dispatch` reports an unresolvable block against and the check name the
# report and the SNS alert carry
STRATEGY_CHECK = "validation.strategy"

# SNS caps the subject; the detail is in the message and the report either way.
SNS_SUBJECT_MAX_LEN = 100
# SNS's own hard limit is 256 KiB; a wide dataset's failed-column list (a
# `max_null_rate_label` miss echoes every column the partition DOES carry) can blow
# past that on its own, and `Publish` rejects the whole message rather than
# truncating it -- so the alert has to fit before it is sent, not after it fails.
SNS_MESSAGE_MAX_BYTES = 250_000

# Top-level keys this job reads. A missing one means the document was written by an
# incompatible stage 1, not something to recompute from the form.
REQUIRED_KEYS = (
    "run_id",
    "env",
    "model_name",
    "upstream",
    "validation",
    "storage",
    "jobs",
)


class ValidateError(Exception):
    """The job cannot judge the dataset: a malformed argument, an unusable compiled
    intake, or a partition it could not read. Distinct from a rejection."""


class DatasetRejected(Exception):
    """The dataset failed the gate. The report is written and the alert is sent by the
    time this is raised; nothing was written to the dataset area (AC-7)."""


# The S3 helpers and the structured logger live in `s3_io.py`, shared with the other
# two stages of this pipeline.
_log = s3_io.log


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Validate each split and write the versioned dataset (stage 4 of "
        "the model-ready-dataset pipeline)."
    )
    parser.add_argument("--compiled-intake-s3-uri", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    # Reuses the delivered helper so both pipelines agree on the format rather than
    # carrying a second definition of it.
    if not intake_schema.is_valid_run_id(args.run_id):
        raise ValidateError(
            f"run_id: {args.run_id!r} must be 'r' followed by 12 lowercase base32 "
            "characters, e.g. r5f3a9c2d1e0b."
        )
    return args


def parse_intake(text):
    """Load the compiled intake and check the keys this job reads."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ValidateError(f"compiled intake is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ValidateError("compiled intake must be a JSON object.")
    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        raise ValidateError("compiled intake is missing key(s): " + ", ".join(missing))
    return document


def read_splits(s3, intake, config, run_id):
    """The three partitions stage 3 wrote, from `jobs.split.output_s3_uri`.

    All three always exist: stage 3 refuses a null `splits.strategy` rather than passing
    the whole frame through as one partition, so there is no "whichever partitions are
    present" case to handle. A missing object is therefore a job failure, not a gate
    failure — this stage could not judge a dataset it could not read.
    """
    prefix = intake["jobs"]["split"]["output_s3_uri"].rstrip("/") + "/"
    splits = {}
    for name in SPLIT_NAMES:
        body = s3_io.read_bytes(
            s3, f"{prefix}{name}.parquet", "jobs.split.output_s3_uri", config.account_id
        )
        splits[name] = pd.read_parquet(io.BytesIO(body))
    _log(
        "splits read",
        runId=run_id,
        inputPrefix=prefix,
        rows={name: len(frame) for name, frame in splits.items()},
    )
    return splits


def finding(split, check, passed, observed, detail):
    """One gate result. `observed` is what AC-8 requires the alert to name."""
    return {
        "split": split,
        "check": check,
        "passed": bool(passed),
        "observed": observed,
        "detail": detail,
    }


def check_min_population(split, frame, minimum):
    """`min_population_size` against THIS partition's row count, not the whole."""
    rows = int(len(frame))
    return finding(
        split,
        "min_population_size",
        rows >= minimum,
        rows,
        f"the {split} partition has {rows} row(s); min_population_size is {minimum} "
        f"and is applied to each split independently (AC-6).",
    )


def check_null_rate_label(split, frame, target_columns, maximum):
    """`max_null_rate_label` per label column — a FRACTION, 0.0-1.0, not a percentage.

    A label column the partition does not carry is reported as a failed check rather
    than crashing the job: it means the dataset cannot be trained on, which is exactly
    what this gate exists to catch, and the DS gets the report either way.
    """
    findings = []
    for column in target_columns:
        if column not in frame.columns:
            findings.append(
                finding(
                    split,
                    f"max_null_rate_label[{column}]",
                    False,
                    None,
                    f"the {split} partition does not carry label column {column!r}, so "
                    f"its null rate cannot be measured. It has "
                    f"{sorted(frame.columns)}.",
                )
            )
            continue
        # An empty partition has no null rate; min_population_size is the check that
        # rejects it, and 0.0 keeps this one from reporting a NaN as the observed value.
        rate = float(frame[column].isna().mean()) if len(frame) else 0.0
        findings.append(
            finding(
                split,
                f"max_null_rate_label[{column}]",
                rate <= maximum,
                rate,
                f"{column!r} is null in {rate:.4f} of the {split} partition; "
                f"max_null_rate_label is {maximum}.",
            )
        )
    return findings


def check_full_observability(split, frame):
    """`require_full_observability` against the cohort's own `label_mature` column.

    Maturity is NOT recomputed here — it is pipeline 1's guard, applied inside every
    label strategy. A partition with no `label_mature` column is a failed check rather
    than a crash: it means the upstream cohort-label run produced labels whose outcome
    window was never proven observed, which is the thing this check is for.
    """
    if MATURITY_COLUMN not in frame.columns:
        return finding(
            split,
            "require_full_observability",
            False,
            None,
            f"the {split} partition carries no {MATURITY_COLUMN!r} column, so maturity "
            f"is unproven — the upstream cohort-label run did not apply it.",
        )
    # A null flag is not proven maturity, so it counts as immature. This also keeps
    # pd.NA out of the comparison, where it would propagate rather than evaluate false.
    immature = int((~frame[MATURITY_COLUMN].fillna(False).astype(bool)).sum())
    return finding(
        split,
        "require_full_observability",
        immature == 0,
        immature,
        f"{immature} of {len(frame)} row(s) in the {split} partition are not mature.",
    )


def check_strategy(split, frame, block):
    """`validation.strategy` — the type-appropriate gate, dispatched.

    The strategy returns nothing and RAISES when the partition fails, so the exception
    is caught and recorded as a finding: propagating it would end the job before the
    report AC-8 requires had been written.

    `engine_dispatch` signals a dispatch problem — an unimportable module, a missing
    function, `params` that do not bind — with `SystemExit`, which is a BaseException
    and so is deliberately NOT caught here. That is not the dataset failing the gate;
    it is this stage being unable to judge it, and it should fail the job loudly.
    """
    strategy = engine_dispatch.describe(block)
    try:
        engine_dispatch.call(block, STRATEGY_CHECK, frame)
    except Exception as exc:  # noqa: BLE001 — any raise from the gate means rejected
        return finding(
            split,
            STRATEGY_CHECK,
            False,
            str(exc),
            f"{strategy} rejected the {split} partition: {exc}",
        )
    return finding(
        split,
        STRATEGY_CHECK,
        True,
        None,
        f"{strategy} accepted the {split} partition.",
    )


def run_gate(splits, validation, target_columns, run_id):
    """All four checks against each split independently (AC-6).

    Every finding is collected rather than short-circuiting on the first failure: the
    report is the artifact a DS reads after the alert, and one that stopped at the first
    problem would send them round the loop once per check.
    """
    minimum = int(validation.get("min_population_size") or 0)
    maximum = float(validation.get("max_null_rate_label") or 0.0)
    strategy = validation.get("strategy")

    findings = []
    for split in SPLIT_NAMES:
        frame = splits[split]
        findings.append(check_min_population(split, frame, minimum))
        findings.extend(check_null_rate_label(split, frame, target_columns, maximum))
        if validation.get("require_full_observability"):
            findings.append(check_full_observability(split, frame))
        if strategy:
            findings.append(check_strategy(split, frame, strategy))

    for entry in findings:
        _log("gate check", runId=run_id, **entry)
    return findings


def dataset_version(dataset_s3_uri):
    """The version segment of `storage.dataset_s3_uri`.

    Read off the URI rather than recomposed from the clock: the compiler fixed the
    version at compile time, and `latest/manifest.json` has to name the same version its
    copy was taken from.
    """
    return dataset_s3_uri.rstrip("/").rsplit("/", 1)[-1]


def build_report(intake, splits, findings, generated_at, dataset_s3_uri):
    """The gate result, persisted on pass AND on fail — the artifact, not just the
    message (AC-8).

    `dataset_s3_uri` is None on a rejection, which is what records that nothing was
    written to the dataset area rather than leaving a reader to infer it.
    """
    validation = intake["validation"]
    strategy = validation.get("strategy")
    return {
        "run_id": intake["run_id"],
        "model_name": intake["model_name"],
        "generated_at": generated_at,
        "upstream_cohort_label_run_id": intake["upstream"].get("cohort_label_run_id"),
        "passed": all(entry["passed"] for entry in findings),
        "target_columns": list(intake["upstream"].get("target_columns") or []),
        "thresholds": {
            "min_population_size": validation.get("min_population_size"),
            "max_null_rate_label": validation.get("max_null_rate_label"),
            "require_full_observability": validation.get("require_full_observability"),
            "strategy": engine_dispatch.describe(strategy) if strategy else None,
        },
        "splits": {
            name: {"rows": int(len(frame)), "columns": int(frame.shape[1])}
            for name, frame in splits.items()
        },
        "checks": findings,
        "dataset_s3_uri": dataset_s3_uri,
    }


def build_manifest(intake, splits, compiled_intake_s3_uri, generated_at, report_s3_uri):
    """The dataset's own descriptor, written beside the three partitions.

    Carries enough to reproduce the dataset and to audit it: which run and which
    upstream cohort produced it, which document drove it, what each partition holds, and
    where the gate result that admitted it lives. No template or AC defines this shape —
    it was agreed with the user rather than invented here.

    The SAME document is written into `latest/`, so its `version` names the version that
    alias currently points at.
    """
    storage = intake["storage"]
    upstream = intake["upstream"]
    return {
        "run_id": intake["run_id"],
        "model_name": intake["model_name"],
        "version": dataset_version(storage["dataset_s3_uri"]),
        "generated_at": generated_at,
        "compiled_intake_s3_uri": compiled_intake_s3_uri,
        "upstream_cohort_label_run_id": upstream.get("cohort_label_run_id"),
        "entity_id_column": upstream.get("entity_id_column"),
        "target_columns": list(upstream.get("target_columns") or []),
        "dataset_s3_uri": storage["dataset_s3_uri"],
        "splits": {
            name: {
                "object": f"{name}.parquet",
                "rows": int(len(frame)),
                "columns": int(frame.shape[1]),
            }
            for name, frame in splits.items()
        },
        "validation": {"passed": True, "report_s3_uri": report_s3_uri},
    }


def write_dataset(s3, splits, manifest, destination, field, config):
    """The three partitions plus the manifest, to one destination. S3 only (AC-9)."""
    prefix = destination.rstrip("/") + "/"
    written = []
    for name in SPLIT_NAMES:
        buffer = io.BytesIO()
        splits[name].to_parquet(buffer, index=False)
        written.append(
            s3_io.put_object(
                s3,
                f"{prefix}{name}.parquet",
                buffer.getvalue(),
                "application/octet-stream",
                field,
                config,
            )
        )
    written.append(
        s3_io.put_object(
            s3,
            prefix + MANIFEST_OBJECT,
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            "application/json",
            field,
            config,
        )
    )
    return written


def publish_rejection(sns, intake, findings, report_s3_uri, run_id):
    """Alert the DS, naming the failing check, the split and the observed value (AC-8).

    The split is named because the thresholds are applied per partition: a form saying
    `min_population_size: 100` fails on holdout long before the cohort is small, and a
    message that did not say which split would look wrong to the author who set it.
    """
    failed = [entry for entry in findings if not entry["passed"]]
    message = "\n".join(
        [
            f"Model-ready dataset REJECTED — {intake['model_name']} (run {run_id})",
            "",
            f"{len(failed)} check(s) failed:",
            *[
                f"  {entry['split']}/{entry['check']}: observed "
                f"{entry['observed']!r} — {entry['detail']}"
                for entry in failed
            ],
            "",
            f"Validation report: {report_s3_uri}",
            "Nothing was written to the dataset area.",
        ]
    )
    subject = (
        f"[{intake['env']}] model-ready dataset REJECTED: {intake['model_name']}"
    )[:SNS_SUBJECT_MAX_LEN]
    if len(message.encode("utf-8")) > SNS_MESSAGE_MAX_BYTES:
        # Trimmed on a full findings list (a check's `detail` can itself be huge,
        # e.g. every column a wide partition carries), not the summary lines around
        # it, so the run/subject/report-URI a reader needs to look further stays
        # intact.
        message = "\n".join(
            [
                f"Model-ready dataset REJECTED — {intake['model_name']} (run {run_id})",
                "",
                f"{len(failed)} check(s) failed. The full list is too large for an "
                f"SNS message; see the report for every failing check.",
                "",
                f"Validation report: {report_s3_uri}",
                "Nothing was written to the dataset area.",
            ]
        )
    sns.publish(
        TopicArn=intake["validation"]["sns_topic_arn"],
        Subject=subject,
        Message=message,
    )
    return message


def main(argv=None):
    args = _parse_args(argv)
    config = job_config.load()
    run_id = args.run_id

    s3 = boto3.client("s3", region_name=config.region)

    intake = parse_intake(
        s3_io.read_text(
            s3, args.compiled_intake_s3_uri, "compiled_intake_s3_uri", config.account_id
        )
    )
    # The run id is the first segment of the object key, so a mismatch means this run
    # was handed another run's document.
    if intake["run_id"] != run_id:
        raise ValidateError(
            f"run_id: argument {run_id!r} does not match the compiled intake's "
            f"{intake['run_id']!r}."
        )
    if intake["env"] != config.env:
        raise ValidateError(
            f"env: the compiled intake was compiled for {intake['env']!r} but this "
            f"job runs in {config.env!r}."
        )

    # The label column(s) the cohort actually carries, in the CASE the joined
    # dataframe actually has them in. Stage 1 only checks source.target_columns
    # against upstream.target_columns case-INSENSITIVELY (output_target_columns is
    # deliberately not case-normalized, since the cohort-label job's own author
    # chose it), so upstream's casing is not guaranteed to match a column the pit
    # join stage lowercased on the way through Snowflake.
    target_columns = list(intake["source"].get("target_columns") or [])
    if not target_columns:
        raise ValidateError(
            "source.target_columns is empty, so there is no label column to measure "
            "max_null_rate_label against and the dataset cannot be gated."
        )

    splits = read_splits(s3, intake, config, run_id)
    findings = run_gate(splits, intake["validation"], target_columns, run_id)
    passed = all(entry["passed"] for entry in findings)

    generated_at = utc_now()
    storage = intake["storage"]
    report = build_report(
        intake,
        splits,
        findings,
        generated_at,
        storage["dataset_s3_uri"] if passed else None,
    )
    # ALWAYS, and before the dataset write: AC-8 requires the report on both paths, and
    # AC-7 is "nothing lands in the dataset area", not "nothing is left there after".
    report_s3_uri = s3_io.put_object(
        s3,
        intake["validation"]["report_s3_uri"],
        json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
        "application/json",
        "validation.report_s3_uri",
        config,
    )

    if not passed:
        failed = [entry for entry in findings if not entry["passed"]]
        sns = boto3.client("sns", region_name=config.region)
        publish_rejection(sns, intake, findings, report_s3_uri, run_id)
        _log(
            "dataset rejected",
            runId=run_id,
            failedChecks=[f"{entry['split']}/{entry['check']}" for entry in failed],
            reportS3Uri=report_s3_uri,
        )
        raise DatasetRejected(
            f"{len(failed)} validation check(s) failed; nothing was written to the "
            f"dataset area. Report: {report_s3_uri}"
        )

    manifest = build_manifest(
        intake, splits, args.compiled_intake_s3_uri, generated_at, report_s3_uri
    )
    # The versioned copy FIRST: `latest/` must never point at a dataset that does not
    # exist, so the immutable artifact lands before the alias is refreshed.
    written = write_dataset(
        s3,
        splits,
        manifest,
        storage["dataset_s3_uri"],
        "storage.dataset_s3_uri",
        config,
    )
    written += write_dataset(
        s3,
        splits,
        manifest,
        storage["dataset_latest_s3_uri"],
        "storage.dataset_latest_s3_uri",
        config,
    )

    _log(
        "dataset written",
        runId=run_id,
        version=manifest["version"],
        rows=report["splits"],
        reportS3Uri=report_s3_uri,
        written=written,
    )
    return report


if __name__ == "__main__":
    main()
