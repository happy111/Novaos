"""preprocess.py — the PLATFORM's preprocessing entry point (M5 `prep`), in-container.

Thin by design. Everything about HOW preprocessing works — resolving the feature
list, fitting the ordered steps on train only, applying them to every partition,
and writing the artifacts — lives in
`ar_transformations.preprocessing.base.ModelPreprocessor`, the same place
`ModelTrainer` and `ModelEvaluator` hold the training and evaluation contracts.
This file owns only what a LIBRARY must not know:

  - the SageMaker Processing mount layout (which parquet sits in which subdir),
  - the step's argument surface, including the 256-char-per-argument cap that
    forces the `--steps-file` form,
  - turning a library exception into one `[prep]` log line and exit 1, because a
    traceback in CloudWatch is noise.

That is the same division cohort-label's engine_dispatch.py and
feature-ingestion's transform_and_ingest.py already use: read the inputs, dispatch
into the library, write nothing of your own.

It is fit-on-TRAIN-only: the fitted transform ships as a model artifact so serving
applies the identical transform. It is NOT feature engineering (M2) and NOT
splitting (M4): those are upstream.

The transform is DATA, not a flag baked into this file. The config authors an
ordered list of {module, function, source_columns, params} steps and the library
imports and composes them, so a DS adds a transform by editing YAML, not this code.
Nothing here is specific to any one model — the pipeline drives it entirely through
the arguments below and the --steps-file it uploads from the config's
preprocessing[] list — which is why it is PLATFORM-owned and lives here once.
Models used to vendor a copy under local_transformations/preprocessing/, so every
one of them carried a file it had to keep in sync and must never edit.
build_and_push.sh now bakes this copy into the preprocessing image whenever the
model supplies no prep source; a model that genuinely needs its own driver still
overrides it with --src-preprocessing.

Container I/O contract (SageMaker Processing):
  inputs  (mounted, read):
    /opt/ml/processing/input/data/{train,validation,holdout}/<the split>.parquet
      (each split is its own mount subdir — SageMaker rejects shared destinations)
  output  (written, uploaded to S3 -> becomes the Train + Evaluate "train" channel):
    /opt/ml/processing/output/prep/
      X_train.npy  y_train.npy      (algorithm/train.py)
      X_val.npy    y_val.npy        (algorithm/train.py)
      X_test.npy   y_test.npy       (evaluation/eval.py)
      split_test.parquet            (evaluation/eval.py: RAW facet+label for bias)
      scaler.pkl  preprocess_meta.json  (train_launcher stages these INTO model.tar.gz)

The partition names above are the LIBRARY's (`ModelPreprocessor.split_stems`), which
is what maps holdout -> the `test` stem the evaluator reads. See that class for why
scaler.pkl is one object, why it is absent on the identity path, and why the
composed transform is indexed by column NAME rather than position.

KNOWN LIMIT: step params travel as JSON, which has no tuple. A transformer whose
parameter validation demands one (MinMaxScaler's feature_range, RobustScaler's
quantile_range) cannot be configured from YAML — it fails at fit with sklearn's own
message naming the parameter. Lists are deliberately NOT coerced to tuples: other
parameters legitimately take lists (OneHotEncoder's categories), so guessing would
break those instead. Such transformers need the YAML contract extended.
"""

import argparse
import json
import os

import pandas as pd
from ar_transformations.preprocessing.base import ModelPreprocessor

_IN_DIR = "/opt/ml/processing/input/data"
_OUT_DIR = "/opt/ml/processing/output/prep"


def _read_splits():
    """Read every partition present under its own mount subdir (_IN_DIR/<name>/).

    Each split mounts into its own directory; the parquet inside keeps its original
    leaf name (train.parquet, etc.), so we read the first .parquet we find there. An
    absent partition is simply omitted — the library skips what it was not given,
    and only the train partition is mandatory.
    """
    splits = {}
    for name in ModelPreprocessor.split_stems:
        split_dir = os.path.join(_IN_DIR, name)
        if not os.path.isdir(split_dir):
            continue
        parquets = [f for f in os.listdir(split_dir) if f.endswith(".parquet")]
        if not parquets:
            continue
        splits[name] = pd.read_parquet(os.path.join(split_dir, parquets[0]))
        print(f"[prep] read {name}: {splits[name].shape}")
    return splits


def _load_steps(inline_json, steps_file):
    """Resolve the ordered step list from either the inline JSON or a mounted file.

    Both forms exist because SageMaker caps ONE ContainerArgument at 256 characters
    and a realistic step list blows past that (a single StandardScaler over 14
    feature columns is already ~430). The pipeline passes --steps-file for anything
    long; --steps stays supported for short lists and for running this script by
    hand. Absent/empty means no transform at all (the identity path).
    """
    if steps_file:
        with open(steps_file) as f:
            raw = f.read()
    else:
        raw = inline_json or ""
    raw = raw.strip()
    if not raw:
        return []
    try:
        steps = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[prep] --steps is not valid JSON: {exc}") from None
    if not isinstance(steps, list):
        raise SystemExit("[prep] --steps must be a JSON list of step objects.")
    return steps


def main():
    parser = argparse.ArgumentParser(description="Model preprocessing (fit-on-train).")
    # Columns are passed as SEPARATE tokens (nargs), not one comma-joined string:
    # SageMaker caps a single ContainerArgument at 256 chars, and a realistic
    # feature list blows past that. Each token stays well under the cap.
    # Optional on purpose: the pipeline omits the flag entirely when the config
    # authors no feature list, and an empty list means "every column in the train
    # split except the target and the drops". Only this container can resolve that,
    # because only it has read the dataset.
    parser.add_argument(
        "--feature-columns",
        default=[],
        nargs="*",
        help="Feature columns, space-separated (the leakage-safe "
        "contract). Blank = all columns except the target and "
        "--drop-columns.",
    )
    parser.add_argument("--target-variable", required=True, help="Label column name.")
    parser.add_argument(
        "--facet-columns",
        default=[],
        nargs="*",
        help="Bias facet columns to carry (raw) into split_test.parquet.",
    )
    # Column filters. Both are applied to the feature list whether it was authored
    # or derived, so an explicit --feature-columns cannot smuggle back a column the
    # config asked to drop.
    parser.add_argument(
        "--drop-columns",
        default=[],
        nargs="*",
        help="Columns to exclude from the features (e.g. identifiers).",
    )
    parser.add_argument(
        "--drop-zero-variance",
        action="store_true",
        help="Also drop features with a single distinct value in the "
        "TRAIN split (measured on train only, applied to all).",
    )
    # The transform list. One of these two; --steps-file wins when both are given,
    # because that is the form the pipeline uses once the JSON outruns the 256-char
    # per-argument cap.
    parser.add_argument(
        "--steps",
        default="",
        help="Ordered transforms as JSON: a list of "
        "{module, function, source_columns, params}. "
        "Empty means no transform (identity).",
    )
    parser.add_argument(
        "--steps-file",
        default="",
        help="Path to a mounted file holding the same JSON, for step "
        "lists longer than one SageMaker container argument.",
    )
    args = parser.parse_args()

    splits = _read_splits()
    if ModelPreprocessor.train_split not in splits:
        raise SystemExit("[prep] train.parquet not found in the input channel.")

    steps = _load_steps(args.steps, args.steps_file)

    # --target-variable stays SINGULAR on the CLI (one label per training run) and
    # becomes the library's target_columns list, which is the shape every other
    # base class takes.
    try:
        preprocessor = ModelPreprocessor(
            splits,
            target_columns=[args.target_variable],
            feature_columns=[c.strip() for c in args.feature_columns if c.strip()],
            drop_columns=[c.strip() for c in args.drop_columns if c.strip()],
            drop_zero_variance=args.drop_zero_variance,
            facet_columns=[c.strip() for c in args.facet_columns if c.strip()],
            steps=steps,
        )
        meta = preprocessor.run(_OUT_DIR)
    except ModuleNotFoundError as exc:
        # Almost always a step naming a class from a package the image lacks.
        raise SystemExit(
            f"[prep] cannot import a step's module — is it installed in this image? "
            f"Add it to compute.preprocess.local_requirements. ({exc})"
        ) from None
    except (ValueError, TypeError, AttributeError) as exc:
        message = str(exc)
        if "sparse" in message.lower():
            raise SystemExit(
                "[prep] a step produced a SPARSE result, and the X_*.npy contract is "
                "dense. Pass sparse_output: false in that step's params. "
                f"(sklearn: {message})"
            ) from None
        raise SystemExit(f"[prep] {message}") from None

    print(
        f"[prep] wrote {meta['n_features']} -> {meta['n_output_features']} column(s) "
        f"to {_OUT_DIR}:"
    )
    for name in sorted(os.listdir(_OUT_DIR)):
        print(f"[prep]   out: {name}")


if __name__ == "__main__":
    main()
