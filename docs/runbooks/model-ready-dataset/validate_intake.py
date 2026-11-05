#!/usr/bin/env python3
"""Validate a model-ready-dataset intake before anything is built, uploaded or run.

Runs the SAME validation stage 1 runs (`src.common.mrd_schema`), so a clean result
here means the compiler Lambda will not reject the form.

Unlike the feature-ingestion and cohort-label validators, this one needs AWS
credentials and S3 read. An MRD form does not restate what the cohort IS — it says
what to do to it — so half the checks are against the UPSTREAM cohort-label run:
the label's target type, the label column(s), the entity id column and where the
cohort landed. Those come from that run's own compiled intake, which is read from
S3 here (SPEC-M4 §2.4). Validating without it would pass forms that a real run
rejects.

The runbook exports two variables:

    export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/\\
        patient_outcome_mortality_v2/model_ready_dataset
    export UPSTREAM=r2bb1b39024fb          # or the full population_labeled.parquet URI

    validate_intake.py                     # reads both
    validate_intake.py <other dir>         # or take the intake path directly

The intake directory holds — a form is located by MODEL alone, one per model, and
its filename is fixed:

    model-ready-dataset-intake-form.yaml
    source/...                      the queries the form's source_query fields name
    local_transformations/...        OPTIONAL — form-local strategy modules

The packaged strategy tier is resolved by IMPORT, so `ar_transformations` has to be
importable — pip-install it from the JFrog PyPI repo, the same distribution
build_and_push.sh bakes into the image. Without it, every strategy the form names
goes UNCHECKED. AR_TRANSFORMATIONS_SRC points at a clone's src/ instead.

Exit codes: 0 valid, 1 the form is invalid, 2 the paths could not be resolved.
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

# Resolved BEFORE the schema import below, which imports the strategy modules a
# form names. Unset is the normal case: the distribution is pip-installed, so it
# imports without help.
_TRANSFORMATIONS_SRC = os.environ.get("AR_TRANSFORMATIONS_SRC", "").strip()
if _TRANSFORMATIONS_SRC:
    sys.path.insert(0, _TRANSFORMATIONS_SRC)

from src.common import mrd_upstream  # noqa: E402  (after path insert)
from src.common.mrd_schema import ERROR, FORM_FILENAME, validate  # noqa: E402

MRD_INTAKE_DIR = "model_ready_dataset"
RUN_ID_RE = re.compile(r"^r[a-z0-9]{12}$")

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNRESOLVED = 2


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate an MRD intake form against its upstream cohort-label run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "intake_dir",
        nargs="?",
        type=pathlib.Path,
        help="the model's model_ready_dataset directory (default: $INTAKE_DIR)",
    )
    parser.add_argument(
        "--upstream",
        default="",
        help="the upstream cohort-label run: its run id, or the full "
        "population_labeled.parquet S3 URI (default: $UPSTREAM)",
    )
    parser.add_argument(
        "--env", default=os.environ.get("ENV", "dev"), help="environment (default: dev)"
    )
    return parser.parse_args(argv)


def derive_model_id(intake_dir):
    """The model the form belongs to: the path above model_ready_dataset/, relative
    to the models repo root.

    That is one or two segments depending on the model, so the root is found with git
    rather than by counting parents. None when it cannot be. Display only here;
    upload_intake.sh needs it for real, because the S3 prefix stage 1 composes is
    built from exactly this.
    """
    if intake_dir.name != MRD_INTAKE_DIR:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(intake_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        root = pathlib.Path(completed.stdout.strip()).resolve()
        return str(intake_dir.parent.relative_to(root))
    except ValueError:
        return None


def expand_upstream(given, env_name):
    """A bare cohort-label run id becomes the parquet URI the pipeline expects.

    The operator has just run the cohort-label pipeline and has its run id in hand;
    the full URI is composed from the environment file so the bucket name is never
    typed. A value that already looks like a URI is used as given.
    """
    given = str(given or "").strip()
    if not given or given.startswith("s3://"):
        return given
    if not RUN_ID_RE.match(given):
        raise ValueError(
            f"--upstream {given!r} is neither a run id (^r[a-z0-9]{{12}}$) nor an "
            f"s3:// URI."
        )
    env_file = REPO_ROOT / "cdk_pipeline" / "environments" / f"{env_name}.json"
    cfg = json.loads(env_file.read_text())
    bucket = (
        f"novartis-{cfg['account']}-{cfg['env']}-{cfg['region']}-"
        f"{cfg['app_prefix']}-cohort-label"
    )
    return f"s3://{bucket}/{given}/population-label/population_labeled.parquet"


def read_upstream(population_labeled_s3_uri):
    """The upstream cohort-label run's facts, read from its own compiled intake."""
    import boto3

    uri, run_id = mrd_upstream.compiled_intake_uri(population_labeled_s3_uri)
    bucket, _, key = uri[len("s3://") :].partition("/")
    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return uri, run_id, mrd_upstream.parse(body.decode("utf-8"), run_id, uri)


def main(argv=None):
    args = _parse_args(argv)
    given = args.intake_dir or os.environ.get("INTAKE_DIR", "").strip()
    if not given:
        print(
            "no intake directory. Export INTAKE_DIR (see the runbook) or pass one "
            "as the only argument.",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVED

    intake_dir = pathlib.Path(given).expanduser().resolve()
    if not intake_dir.is_dir():
        print(f"{intake_dir} does not exist", file=sys.stderr)
        return EXIT_UNRESOLVED
    form_path = intake_dir / FORM_FILENAME
    if not form_path.is_file():
        print(
            f"{intake_dir} has no {FORM_FILENAME}; stage 1 opens it by that exact "
            f"name. Is INTAKE_DIR a <model>/{MRD_INTAKE_DIR} directory?",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVED

    upstream_given = args.upstream or os.environ.get("UPSTREAM", "")
    if not upstream_given:
        print(
            "no upstream cohort-label run. Export UPSTREAM with that run's id (or "
            "its population_labeled.parquet URI) — the form is checked against the "
            "cohort it will consume, so there is nothing to validate without it.",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVED

    try:
        population_labeled = expand_upstream(upstream_given, args.env)
        upstream_uri, upstream_run_id, upstream = read_upstream(population_labeled)
    except Exception as exc:  # noqa: BLE001 — the message is the whole point
        print(f"upstream could not be read: {exc}", file=sys.stderr)
        return EXIT_UNRESOLVED

    try:
        form = yaml.safe_load(form_path.read_text())
    except yaml.YAMLError as exc:
        print(f"INVALID — {FORM_FILENAME} is not valid YAML: {exc}", file=sys.stderr)
        return EXIT_INVALID

    model_id = derive_model_id(intake_dir)
    if model_id:
        print(f"model         : {model_id}")
    print(f"intake form   : {form_path}")
    print(f"upstream run  : {upstream_run_id}  ({upstream_uri})")
    print(
        f"upstream facts: target_type={upstream.target_type!r} "
        f"family={upstream.family!r} label={upstream.target_columns} "
        f"entity_id={upstream.entity_id_column!r}"
    )
    print(
        "strategies    : "
        + (
            f"imported from {_TRANSFORMATIONS_SRC}"
            if _TRANSFORMATIONS_SRC
            else "imported from the installed transformations distribution"
        )
    )
    print()

    issues = validate(form, str(intake_dir), upstream)
    errors = [i for i in issues if i.level == ERROR]
    for issue in issues:
        print(f"  {issue.level} {issue.field}: {issue.message}")
    if issues:
        print()

    if errors:
        print(f"INVALID — {len(errors)} error(s). Nothing would be created.")
        return EXIT_INVALID

    # The Lambda currently runs without the distribution, so it downgrades an
    # unresolvable packaged block to a warning. Here that is worth shouting about:
    # this script is the one place the real signatures CAN be checked, so a skipped
    # check means the operator has no signature coverage at all.
    if any("could not be checked" in i.message for i in issues):
        print("VALID — but some strategies went UNCHECKED.")
        print()
        print(
            "No strategy distribution is importable, so those function names and "
            "params were not verified against the real signatures. pip-install "
            "ar_transformations, or set AR_TRANSFORMATIONS_SRC to a clone's src/, "
            "and re-run."
        )
        return EXIT_OK

    print("VALID — stage 1 will accept this form.")
    print()
    print("Next: build_and_push.sh, then upload_intake.sh.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
