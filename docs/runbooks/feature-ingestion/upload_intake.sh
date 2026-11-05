#!/usr/bin/env bash
# Upload one feature group's intake directory to the S3 config bucket, at the
# exact prefix the state machine composes from model_id and feature_group_name.
#
# Run from anywhere, with your feature group's intake directory exported once (see
# the runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/...
#   pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/upload_intake.sh
#
#   ENV           environment file to read, dev | qa | prod   (default: dev)
#
# The bucket, region, account and CMK all come from
# cdk_pipeline/environments/<ENV>.json — never from this script — and the prefix
# is `feature-intake/<model_id>/<feature_group_name>/`, which is NOT arbitrary:
# the state machine composes precisely this path, so changing it here breaks the
# model_id execution input. Both halves are read off INTAKE_DIR, which is why it
# must be the real <brand>/<model>/feature_intake/<group> directory.
#
# Prints the execution input to paste into the Step Functions console when done.
set -euo pipefail

INTAKE_DIR="${INTAKE_DIR:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir)    INTAKE_DIR="$2";         shift 2 ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_NAME="${ENV:-dev}"
ENV_FILE="${REPO_ROOT}/cdk_pipeline/environments/${ENV_NAME}.json"
[[ -f "$ENV_FILE" ]] || { echo "[fail] no such environment file: $ENV_FILE" >&2; exit 1; }

# The S3 prefix is composed from model_id and feature_group_name, so both are read
# back off the path: .../<brand>/<model>/feature_intake/<group>. A directory in any
# other shape would upload to a prefix the state machine never looks at.
FEATURE_GROUP_NAME="$(basename "$INTAKE_DIR")"
if [[ "$(basename "$(dirname "$INTAKE_DIR")")" != "feature_intake" ]]; then
  echo "[fail] INTAKE_DIR must be a <brand>/<model>/feature_intake/<group> directory," >&2
  echo "       so model_id and feature_group_name can be read from it. Got:" >&2
  echo "         $INTAKE_DIR" >&2
  exit 1
fi
MODEL_DIR="$(dirname "$(dirname "$INTAKE_DIR")")"
MODEL_ID="$(basename "$(dirname "$MODEL_DIR")")/$(basename "$MODEL_DIR")"
echo "[info] model_id           : $MODEL_ID"
echo "[info] feature_group_name : $FEATURE_GROUP_NAME"

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
PREFIX="feature-intake/${MODEL_ID}/${FEATURE_GROUP_NAME}"
TARGET="s3://${BUCKET}/${PREFIX}/"

FORM_FILENAME="feature-intake-form.yaml"
if [[ ! -f "$INTAKE_DIR/$FORM_FILENAME" ]]; then
  echo "[fail] $INTAKE_DIR holds no $FORM_FILENAME; stage 1 discovers the form" >&2
  echo "       by that exact name." >&2
  exit 1
fi

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

[ok] Start the state machine (${APP_PREFIX}-feature-onboarding-${ENV_NAME}) with EITHER:

{"model_id": "${MODEL_ID}", "feature_group_name": "${FEATURE_GROUP_NAME}"}

or:

{"feature_intake_s3_uri": "${TARGET}"}
EOF
