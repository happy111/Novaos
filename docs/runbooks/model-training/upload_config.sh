#!/usr/bin/env bash
# Upload one model's model-training directory to the S3 config bucket, at the prefix
# run_pipeline.sh composes from model_id.
#
# Run from anywhere, with the model's model-training directory exported once (see
# the runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/patient_outcome_mortality_v2/model_training
#   pwsa0002037_novaos_ar/docs/runbooks/model-training/upload_config.sh
#
#   --onboarding-form PATH  OPTIONAL. Defaults to the sibling model_onboarding/
#                           directory when it holds a form. Uploaded beside the
#                           config so the Model Card can be seeded from it.
#   ENV                     environment file to read, dev | qa | prod (default: dev)
#
# The bucket, region, account and CMK all come from
# cdk_pipeline/environments/<ENV>.json — never from this script — and the target is
# `<model-training bucket>/configs/<model_id>/`. That is the MODEL-TRAINING bucket,
# not the shared config bucket the other three pipelines use; see the note beside the
# bucket name below for why it has to be.
#
# Unlike the other pipelines that prefix is NOT load-bearing: the state machine is
# handed the config's full S3 URI (`config_s3_uri`), so no Lambda composes a path
# from a model id and nothing breaks if this moves. It is a convention so that
# run_pipeline.sh can DERIVE that URI from INTAKE_DIR instead of asking for it.
#
# The whole directory goes up, not just the YAML. The pipeline only reads the config
# (and the onboarding form); the model's algorithm/ and evaluation/ code is already
# baked into the images by build_and_push.sh. They are uploaded anyway so the
# committed source of a run is recoverable from the run's own inputs, which is the
# same reason the other runbooks upload their whole intake directory.
#
# Prints the execution input to paste into the Step Functions console when done.
set -euo pipefail

INTAKE_DIR="${INTAKE_DIR:-}"
ONBOARDING_FORM=""
ENV_NAME="${ENV:-dev}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir)      INTAKE_DIR="$2";      shift 2 ;;
    --onboarding-form) ONBOARDING_FORM="$2"; shift 2 ;;
    --env)             ENV_NAME="$2";        shift 2 ;;
    -h|--help)         sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "[fail] unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$INTAKE_DIR" ]]; then
  echo "[fail] no model-training directory. Export INTAKE_DIR (see the runbook)." >&2
  exit 1
fi
[[ -d "$INTAKE_DIR" ]] ||
  { echo "[fail] INTAKE_DIR is not a directory: $INTAKE_DIR" >&2; exit 1; }
INTAKE_DIR="$(cd "$INTAKE_DIR" && pwd)"

CONFIG_FILENAME="model-training-config.yaml"
ONBOARDING_FILENAME="model-onboarding-form.yaml"

[[ -f "$INTAKE_DIR/$CONFIG_FILENAME" ]] ||
  { echo "[fail] $INTAKE_DIR has no $CONFIG_FILENAME; the Validate state opens it by that exact name." >&2
    exit 1; }

# The model is the path above model_training/ — one or two segments depending on the
# model — so the models repo root comes from git rather than from counting parents.
if [[ "$(basename "$INTAKE_DIR")" != "model_training" ]]; then
  echo "[fail] INTAKE_DIR must be a <model>/model_training directory, so model_id" >&2
  echo "       can be read from it. Got:" >&2
  echo "         $INTAKE_DIR" >&2
  exit 1
fi
MODELS_REPO="$(git -C "$INTAKE_DIR" rev-parse --show-toplevel)" ||
  { echo "[fail] $INTAKE_DIR is not inside a clone of the sample-models repo." >&2; exit 1; }
MODEL_ID="$(dirname "${INTAKE_DIR#"$MODELS_REPO"/}")"
echo "[info] model_id : $MODEL_ID"

# The onboarding form is about the MODEL, not this training run, so the models repo
# keeps it in a sibling directory rather than inside model_training/.
if [[ -z "$ONBOARDING_FORM" ]]; then
  SIBLING="$INTAKE_DIR/../model_onboarding/$ONBOARDING_FILENAME"
  [[ -f "$SIBLING" ]] && ONBOARDING_FORM="$(cd "$(dirname "$SIBLING")" && pwd)/$ONBOARDING_FILENAME"
fi
if [[ -n "$ONBOARDING_FORM" ]]; then
  [[ -f "$ONBOARDING_FORM" ]] ||
    { echo "[fail] no onboarding form at: $ONBOARDING_FORM" >&2; exit 1; }
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_FILE="${REPO_ROOT}/cdk_pipeline/environments/${ENV_NAME}.json"
[[ -f "$ENV_FILE" ]] || { echo "[fail] no such environment file: $ENV_FILE" >&2; exit 1; }

eval "$(python3 - "$ENV_FILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
machine = (cfg.get("state_machines") or {}).get("model_training") or {}
print(f'ACCOUNT="{cfg["account"]}"')
print(f'REGION="{cfg["region"]}"')
print(f'APP_PREFIX="{cfg["app_prefix"]}"')
print(f'KMS="{cfg["kms_key_arn"]}"')
# Unlike the other pipelines this is the FULL state machine name, used as-is.
print(f'MACHINE="{machine.get("name", "")}"')
PY
)"

# The MODEL-TRAINING bucket, not the shared config bucket the other three pipelines
# upload to. The two pipeline Lambdas run as the generic novos-ar-lambda-execution
# role, whose only S3 grant is this bucket (cdk_iam_deploy_roles.py, sid
# S3ModelTrainingArtifacts) — a config in the config bucket is read with a 403 at the
# first state. Copies of these files DO sit in the config bucket from an earlier
# attempt; nothing can read them.
#
# `configs/`, not `model-training/`: within this bucket `model-training/` is
# AR_ARTIFACT_PREFIX, where the pipeline writes its own per-run outputs. A config is
# an input and does not belong under the output prefix.
BUCKET="novartis-${ACCOUNT}-${ENV_NAME}-${REGION}-${APP_PREFIX}-model-training"
TARGET="s3://${BUCKET}/configs/${MODEL_ID}/"

echo "[info] uploading $INTAKE_DIR"
echo "[info]        -> $TARGET"
aws s3 cp "$INTAKE_DIR/" "$TARGET" --recursive \
  --exclude "*__pycache__/*" \
  --exclude "*.pyc" \
  --sse aws:kms --sse-kms-key-id "$KMS" \
  --only-show-errors

if [[ -n "$ONBOARDING_FORM" ]]; then
  echo "[info] onboarding form: $ONBOARDING_FORM"
  aws s3 cp "$ONBOARDING_FORM" "${TARGET}${ONBOARDING_FILENAME}" \
    --sse aws:kms --sse-kms-key-id "$KMS" \
    --only-show-errors
fi

echo
echo "[ok] uploaded:"
aws s3 ls "$TARGET" --recursive | sed 's/^/  /'

CONFIG_URI="${TARGET}${CONFIG_FILENAME}"
cat <<EOF

[ok] Start the state machine (${MACHINE}) with:

{"config_s3_uri": "${CONFIG_URI}"}
EOF

if [[ -n "$ONBOARDING_FORM" ]]; then
  cat <<EOF

or, to seed the Model Card from the onboarding form:

{"config_s3_uri": "${CONFIG_URI}", "onboarding_form_s3_uri": "${TARGET}${ONBOARDING_FILENAME}"}
EOF
else
  cat <<EOF

[warn] No onboarding form was uploaded, so the registered version gets no Model
       Card and the training gate is not checked against the approved success
       criteria. Pass --onboarding-form to include one.
EOF
fi

echo
echo "[ok] Next: run_pipeline.sh — it composes this same URI from INTAKE_DIR."
