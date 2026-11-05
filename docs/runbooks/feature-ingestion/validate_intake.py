#!/usr/bin/env python3
"""Validate a feature intake before anything is built, uploaded or run.

Runs the SAME validation stage 1 runs (`src.common.intake_schema`), so a clean
result here means the compiler Lambda will not reject the form. Needs no AWS
credentials, no Snowflake and no environment configuration — it reads the form
and its staged intake directory off disk, and (when `ar_transformations` is
installed here) resolves every feature's transformation against its real
signature.

Point it at your feature group's intake directory, which the runbook exports once:

    export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/\\
        patient_outcome_mortality_v2/feature_intake/<feature group>

    validate_intake.py                  # reads $INTAKE_DIR
    validate_intake.py <other dir>      # or take the path directly

That directory holds:

    feature-intake-form.yaml    the feature-intake form, under that exact name
    *.sql                       the source query, resolved via the form's
                                 source.source_query when that names a file
    local_transformations/      OPTIONAL — form-local transformation modules

Exit codes: 0 valid, 1 the form is invalid, 2 the paths could not be resolved.
"""

import argparse
import os
import pathlib
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.common import intake_schema  # noqa: E402  (after path insert)

FEATURE_INTAKE_DIR = "feature_intake"
# ar-<wl>-<env>-fgr- is composed by the compiler and counts against the 64-char
# CreateFeatureGroup limit, so the form's own name has this much room.
FEATURE_GROUP_NAME_BUDGET = 49

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNRESOLVED = 2


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate a feature intake form and its source SQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "intake_dir",
        nargs="?",
        type=pathlib.Path,
        help="the feature group's intake directory (default: $INTAKE_DIR)",
    )
    return parser.parse_args(argv)


def derive_identity(intake_dir):
    """('<brand>/<model>', '<group>') from .../<brand>/<model>/feature_intake/<group>.

    (None, None) when the directory is not in that layout. Only used for display
    here; `upload_intake.sh` needs it for real, because the S3 prefix the state
    machine composes is built from exactly these two.
    """
    parts = intake_dir.parts
    if len(parts) < 4 or parts[-2] != FEATURE_INTAKE_DIR:
        return None, None
    return f"{parts[-4]}/{parts[-3]}", parts[-1]


def _one_file(directory, pattern, label):
    """The single file matching `pattern`, or a message saying why not."""
    matches = sorted(p for p in directory.glob(pattern) if p.is_file())
    if not matches:
        return None, f"no {label} matching {pattern} in {directory}"
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        return (
            None,
            f"expected one {label} in {directory}, found {len(matches)}: {names}",
        )
    return matches[0], None


def resolve_inputs(directory):
    """(form path, sql path, error). The SQL is taken from source.source_query
    when that field names a file that exists, because that is what the form
    itself declares; a lone *.sql is accepted as a fallback."""
    if not directory.is_dir():
        return None, None, f"{directory} does not exist"

    form_path = directory / intake_schema.FORM_FILENAME
    if not form_path.is_file():
        return (
            None,
            None,
            f"no {intake_schema.FORM_FILENAME} in {directory}. The form is "
            "discovered by that exact name.",
        )

    declared = ""
    try:
        parsed = yaml.safe_load(form_path.read_text())
        if isinstance(parsed, dict):
            declared = intake_schema.query_block(
                (parsed.get("source") or {}).get("source_query")
            )["path"]
    except yaml.YAMLError:
        # A form that will not parse is reported by validate(), not here.
        pass

    if declared:
        candidates = [directory / declared]
        candidates += sorted(directory.rglob(pathlib.Path(declared).name))
        for candidate in candidates:
            if candidate.is_file():
                return form_path, candidate, None

    sql_path, error = _one_file(directory, "**/*.sql", "source SQL")
    if error:
        hint = f" source.source_query.path is {declared!r}." if declared else ""
        return None, None, error + hint
    return form_path, sql_path, None


def name_length_warning(form):
    """The one failure validate() cannot see: the composed name is too long.

    The compiler raises on this, but only once it runs, so it is worth catching
    on the operator's machine instead.
    """
    if not isinstance(form, dict):
        return None
    name = str((form.get("identity") or {}).get("feature_group_name") or "").strip()
    if len(name) > FEATURE_GROUP_NAME_BUDGET:
        return (
            f"identity.feature_group_name is {len(name)} characters; the compiler "
            f"prefixes ar-<workload>-<env>-fgr- and CreateFeatureGroup allows 64, "
            f"so keep it to {FEATURE_GROUP_NAME_BUDGET}."
        )
    return None


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
    form_path, sql_path, error = resolve_inputs(intake_dir)
    if error:
        print(error, file=sys.stderr)
        return EXIT_UNRESOLVED

    try:
        form = yaml.safe_load(form_path.read_text())
    except yaml.YAMLError as exc:
        print(f"INVALID — {form_path.name} is not valid YAML: {exc}", file=sys.stderr)
        return EXIT_INVALID

    model_id, feature_group = derive_identity(intake_dir)
    if model_id:
        print(f"model         : {model_id}")
        print(f"feature group : {feature_group}")
    print(f"intake form   : {form_path}")
    print(f"source sql    : {sql_path}")
    print()

    # `intake_dir` lets validate() also check every feature's transformation
    # against its real signature (ar_transformations.utils.dispatch et al.,
    # when that distribution is installed here) and confirm a form-local
    # local_transformations/<name>.py module is actually staged beside the form.
    errors = intake_schema.validate(form, str(intake_dir))
    if errors:
        print(f"INVALID — {len(errors)} error(s). Nothing would be created:")
        for error in errors:
            print(f"  {error.field}: {error.message}")
        return EXIT_INVALID

    warning = name_length_warning(form)
    if warning:
        print(f"WARNING: {warning}")

    print("VALID — stage 1 will accept this form.")
    print()
    print("Next: build and push the image, then upload these two objects to S3:")
    print(f"  {form_path.name}")
    print(f"  {sql_path.name}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
