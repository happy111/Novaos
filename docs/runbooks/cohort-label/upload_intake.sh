#!/usr/bin/env bash
# Upload one model's cohort-label intake directory to the S3 config bucket, at the
# exact prefix the compiler Lambda composes from model_id.
#
# Run from anywhere, with the model's cohort-label intake directory exported once
# (see the runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/cosentyx/cohort_label_intake
#   pwsa0002037_novaos_ar/docs/runbooks/cohort-label/upload_intake.sh
#
#   ENV           environment file to read, dev | qa | prod   (default: dev)
#
# The bucket, region, account and CMK all come from
# cdk_pipeline/environments/<ENV>.json — never from this script — and the prefix is
# `cohort-label-intake/<model_id>/`, which is NOT arbitrary: the Lambda composes
# precisely this path, so changing it here breaks the model_id execution input.
# model_id is read off INTAKE_DIR, which is why it must be the real
# <model>/cohort_label_intake directory.
#
# A cohort-label form is located by MODEL alone, and its filename is fixed, so
# unlike a feature intake there is no "exactly one YAML" rule — extra YAMLs beside
# the form are ignored rather than fatal.
#
# Prints the execution input to paste into the Step Functions console when done.
set -euo pipefail

INTAKE_DIR="${INTAKE_DIR:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir) INTAKE_DIR="$2"; shift 2 ;;
    -h|--help)    sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "[fail] unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$INTAKE_DIR" ]]; then
  echo "[fail] no intake directory. Export INTAKE_DIR (see the runbook)." >&2
  exit 1
fi
[[ -d "$INTAKE_DIR" ]] ||
  { echo "[fail] INTAKE_DIR is not a directory: $INTAKE_DIR" >&2; exit 1; }
INTAKE_DIR="$(cd "$INTAKE_DIR" && pwd)"

FORM="cohort-label-intake-form.yaml"
if [[ ! -f "$INTAKE_DIR/$FORM" ]]; then
  echo "[fail] $INTAKE_DIR has no $FORM; stage 1 opens it by that exact name." >&2
  exit 1
fi

# The S3 prefix is composed from model_id, so it is read back off the path: the
# model is the path above cohort_label_intake/ — one or two segments depending on
# the model — so the models repo root comes from git rather than from counting
# parents. A directory in any other shape would upload to a prefix the Lambda
# never looks at.
if [[ "$(basename "$INTAKE_DIR")" != "cohort_label_intake" ]]; then
  echo "[fail] INTAKE_DIR must be a <model>/cohort_label_intake directory, so" >&2
  echo "       model_id can be read from it. Got:" >&2
  echo "         $INTAKE_DIR" >&2
  exit 1
fi
MODELS_REPO="$(git -C "$INTAKE_DIR" rev-parse --show-toplevel)" ||
  { echo "[fail] $INTAKE_DIR is not inside a clone of the sample-models repo." >&2; exit 1; }
MODEL_ID="$(dirname "${INTAKE_DIR#"$MODELS_REPO"/}")"
echo "[info] model_id : $MODEL_ID"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_NAME="${ENV:-dev}"
ENV_FILE="${REPO_ROOT}/cdk_pipeline/environments/${ENV_NAME}.json"
[[ -f "$ENV_FILE" ]] || { echo "[fail] no such environment file: $ENV_FILE" >&2; exit 1; }

eval "$(python3 - "$ENV_FILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
print(f'ACCOUNT="{cfg["account"]}"')
print(f'REGION="{cfg["region"]}"')
print(f'APP_PREFIX="{cfg["app_prefix"]}"')
print(f'KMS="{cfg["kms_key_arn"]}"')
PY
)"

BUCKET="novartis-${ACCOUNT}-${ENV_NAME}-${REGION}-${APP_PREFIX}-config"
TARGET="s3://${BUCKET}/cohort-label-intake/${MODEL_ID}/"

echo "[info] uploading $INTAKE_DIR"
echo "[info]        -> $TARGET"
aws s3 cp "$INTAKE_DIR/" "$TARGET" --recursive \
  --exclude "*__pycache__/*" \
  --exclude "*.pyc" \
  --sse aws:kms --sse-kms-key-id "$KMS" \
  --only-show-errors

echo
echo "[ok] uploaded:"
aws s3 ls "$TARGET" --recursive | sed 's/^/  /'

cat <<EOF

[ok] Start the state machine (${APP_PREFIX}-cohort-label-${ENV_NAME}) with EITHER:

{"model_id": "${MODEL_ID}"}

or:

{"cohort_label_intake_s3_uri": "${TARGET}"}

Add "as_of": "<ISO timestamp>" to pin the reference date the windowing resolves
against; it defaults to now, so omitting it makes the run unreproducible.
EOF
