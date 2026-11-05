#!/usr/bin/env python3
"""Validate a cohort-label intake before anything is built, uploaded or run.

Runs the SAME validation stage 1 runs (`src.common.cohort_label_schema`), so a
clean result here means the compiler Lambda will not reject the form. Needs no AWS
credentials and no Snowflake — it reads the form and the files it names off disk.

Point it at the model's cohort-label intake directory, which the runbook exports
once:

    export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/cosentyx/\\
        cohort_label_intake

    validate_intake.py                  # reads $INTAKE_DIR
    validate_intake.py <other dir>      # or take the path directly

That directory holds — a cohort-label form is located by MODEL alone, one form per
model, and its filename is fixed:

    cohort-label-intake-form.yaml   the form
    sql/...                         the queries the form's source_query fields name
    local_transformations/...       OPTIONAL — form-local strategy modules

The packaged strategy tier is resolved by IMPORT, so `ar_transformations` has to be
importable — pip-install it from the JFrog PyPI repo, the same distribution
build_and_push.sh bakes into the image. Without it, every strategy the form names
goes UNCHECKED. AR_TRANSFORMATIONS_SRC points at a clone's src/ instead, which is
what the test suite does; the signatures it checks are then that clone's, not the
published distribution's.

Exit codes: 0 valid, 1 the form is invalid, 2 the paths could not be resolved.
"""

import argparse
import os
import pathlib
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

from src.common.cohort_label_schema import (  # noqa: E402  (after path insert)
    ERROR,
    FORM_FILENAME,
    validate,
)

COHORT_LABEL_INTAKE_DIR = "cohort_label_intake"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNRESOLVED = 2


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate a cohort-label intake form and the files it names.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "intake_dir",
        nargs="?",
        type=pathlib.Path,
        help="the model's cohort_label_intake directory (default: $INTAKE_DIR)",
    )
    return parser.parse_args(argv)


def derive_model_id(intake_dir):
    """The model the form belongs to: the path above cohort_label_intake/, relative
    to the models repo root.

    That is one or two segments depending on the model — cosentyx keeps its form at
    <brand>/, brand_abc at <brand>/<model>/ — so the root is found with git rather
    than by counting parents. None when it cannot be. Display only here;
    upload_intake.sh needs it for real, because the S3 prefix stage 1 composes is
    built from exactly this.
    """
    if intake_dir.name != COHORT_LABEL_INTAKE_DIR:
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


def resolve_form(intake_dir):
    """(form path, error). The form's name is fixed, so there is nothing to guess."""
    if not intake_dir.is_dir():
        return None, f"{intake_dir} does not exist"
    form_path = intake_dir / FORM_FILENAME
    if not form_path.is_file():
        return None, (
            f"{intake_dir} has no {FORM_FILENAME}; stage 1 opens it by that exact "
            f"name. Is INTAKE_DIR a <model>/{COHORT_LABEL_INTAKE_DIR} directory?"
        )
    return form_path, None


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
    form_path, error = resolve_form(intake_dir)
    if error:
        print(error, file=sys.stderr)
        return EXIT_UNRESOLVED

    try:
        form = yaml.safe_load(form_path.read_text())
    except yaml.YAMLError as exc:
        print(f"INVALID — {FORM_FILENAME} is not valid YAML: {exc}", file=sys.stderr)
        return EXIT_INVALID

    model_id = derive_model_id(intake_dir)
    if model_id:
        print(f"model       : {model_id}")
    print(f"intake form : {form_path}")
    print(
        "strategies  : "
        + (
            f"imported from {_TRANSFORMATIONS_SRC}"
            if _TRANSFORMATIONS_SRC
            else "imported from the installed transformations distribution"
        )
    )
    print()

    issues = validate(form, str(intake_dir))
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
        print("VALID — but every strategy went UNCHECKED.")
        print()
        print(
            "No strategy distribution is importable, so the function names and the "
            "params in this form were not verified against the real signatures. "
            "Clone pwsa0002037_novaos_ar_transformations beside this repo, or set "
            "AR_TRANSFORMATIONS_SRC to a clone's src/, and re-run."
        )
        return EXIT_OK

    print("VALID — stage 1 will accept this form.")
    print()
    print("Next: build_and_push.sh, then upload_intake.sh.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
