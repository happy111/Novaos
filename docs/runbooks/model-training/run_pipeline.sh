#!/usr/bin/env bash
# Start the model-training state machine for one model and follow it to a terminal
# state.
#
# Run from anywhere, with the model's model-training directory exported once (see
# the runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/patient_outcome_mortality_v2/model_training
#   pwsa0002037_novaos_ar/docs/runbooks/model-training/run_pipeline.sh
#
#   --onboarding-form-uri S3  OPTIONAL. Seed the Model Card from this form and check
#                             the training gate against its approved criteria.
#                             Default: the one upload_config.sh put beside the
#                             config, when it is there.
#   --no-onboarding-form      Start without one even if it is in S3.
#   --no-wait                 Start it and print the execution ARN, without following.
#   ENV                       environment file to read, dev | qa | prod (default: dev)
#
# The execution input is `{config_s3_uri}` (plus the optional
# `onboarding_form_s3_uri`), composed from INTAKE_DIR exactly as upload_config.sh
# composes the upload prefix — which is why it must be the real
# <model>/model_training directory. Upload the config first: this starts a run
# against whatever is in S3 now, not against your working tree.
#
# Build the images first too (build_and_push.sh). The pipeline resolves each step's
# image from identity.model_name and pulls whatever that repo:tag points at now, so
# a run against a stale tag silently trains old code rather than failing.
#
# Three states run in series: Validate (Lambda), Build & Launch (Lambda), then a
# poll loop over the SageMaker Pipeline execution the launch state started. The five
# pipeline steps run INSIDE that execution, so a step failure surfaces here as a
# failed pipeline execution rather than a failed state.
#
# For a dry run — validate and build the definition without creating anything — use
# the local harness instead, which needs the environment in your own shell:
#   set -a; source .env; set +a
#   python3 scripts/run_training_pipeline.py --config <path> --dry-run
#
# Exit code is 0 only when the execution SUCCEEDED.
set -euo pipefail

INTAKE_DIR="${INTAKE_DIR:-}"
ENV_NAME="${ENV:-dev}"
ONBOARDING_URI=""
USE_ONBOARDING=1
WAIT=1
# How often to ask, and how long before giving up following (the run itself is
# unaffected — the state machine has its own timeout_hours). Training is the longest
# stage of the platform, so the default ceiling is higher than the other runbooks'.
POLL_SECONDS="${POLL_SECONDS:-60}"
FOLLOW_TIMEOUT_SECONDS="${FOLLOW_TIMEOUT_SECONDS:-14400}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir)            INTAKE_DIR="$2";     shift 2 ;;
    --onboarding-form-uri)   ONBOARDING_URI="$2"; shift 2 ;;
    --no-onboarding-form)    USE_ONBOARDING=0;    shift ;;
    --env)                   ENV_NAME="$2";       shift 2 ;;
    --no-wait)               WAIT=0;              shift ;;
    -h|--help)               sed -n '2,40p' "$0"; exit 0 ;;
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

# Same derivation as upload_config.sh, and for the same reason: the config's S3 URI
# is composed from model_id, so it must be the directory's own model path.
if [[ "$(basename "$INTAKE_DIR")" != "model_training" ]]; then
  echo "[fail] INTAKE_DIR must be a <model>/model_training directory, so model_id" >&2
  echo "       can be read from it. Got:" >&2
  echo "         $INTAKE_DIR" >&2
  exit 1
fi
MODELS_REPO="$(git -C "$INTAKE_DIR" rev-parse --show-toplevel)" ||
  { echo "[fail] $INTAKE_DIR is not inside a clone of the sample-models repo." >&2; exit 1; }
MODEL_ID="$(dirname "${INTAKE_DIR#"$MODELS_REPO"/}")"

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
print(f'MACHINE="{machine.get("name", "")}"')
PY
)"

if [[ -z "$MACHINE" ]]; then
  echo "[fail] $ENV_FILE has no state_machines.model_training.name." >&2
  exit 1
fi

# Composed the same way every other pipeline's run_pipeline.sh does:
# cdk_model_training_state_machine.py names the deployed state machine
# <app_prefix>-<name>-<env>, so dev.json's state_machines.model_training.name is
# only the middle segment, not the full name.
STATE_MACHINE_ARN="arn:aws:states:${REGION}:${ACCOUNT}:stateMachine:${APP_PREFIX}-${MACHINE}-${ENV_NAME}"

# The model-training bucket, not the shared config bucket: it is the only bucket the
# pipeline Lambdas can read. Must match upload_config.sh — see the note there.
CONFIG_BUCKET="novartis-${ACCOUNT}-${ENV_NAME}-${REGION}-${APP_PREFIX}-model-training"
PREFIX="s3://${CONFIG_BUCKET}/configs/${MODEL_ID}"
CONFIG_S3_URI="${PREFIX}/model-training-config.yaml"

aws s3 ls "$CONFIG_S3_URI" >/dev/null 2>&1 || {
  echo "[fail] no config at $CONFIG_S3_URI" >&2
  echo "       Run upload_config.sh first — this starts a run against S3, not against" >&2
  echo "       your working tree." >&2
  exit 1
}

# Only include the key when the form is actually there. The Validate state always
# returns it (null when absent) and the Launch state reads it from THAT output, so
# omitting it from the execution input is safe.
if [[ "$USE_ONBOARDING" -eq 1 && -z "$ONBOARDING_URI" ]]; then
  CANDIDATE="${PREFIX}/model-onboarding-form.yaml"
  aws s3 ls "$CANDIDATE" >/dev/null 2>&1 && ONBOARDING_URI="$CANDIDATE"
fi
[[ "$USE_ONBOARDING" -eq 0 ]] && ONBOARDING_URI=""

INPUT="$(python3 - "$CONFIG_S3_URI" "$ONBOARDING_URI" <<'PY'
import json, sys
config_uri, onboarding_uri = sys.argv[1], sys.argv[2]
payload = {"config_s3_uri": config_uri}
if onboarding_uri:
    payload["onboarding_form_s3_uri"] = onboarding_uri
print(json.dumps(payload))
PY
)"

echo "[info] state machine : ${APP_PREFIX}-${MACHINE}-${ENV_NAME}"
echo "[info] model_id      : $MODEL_ID"
echo "[info] input         : $INPUT"
[[ -n "$ONBOARDING_URI" ]] ||
  echo "[warn] no onboarding form: the registered version gets no Model Card, and the training gate is not checked against the approved criteria."

EXECUTION_ARN="$(aws stepfunctions start-execution \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --input "$INPUT" \
  --query executionArn --output text)"

echo "[ok]   started       : $EXECUTION_ARN"
echo "[info] console       : https://${REGION}.console.aws.amazon.com/states/home?region=${REGION}#/v2/executions/details/${EXECUTION_ARN}"

if [[ "$WAIT" -eq 0 ]]; then
  echo
  echo "[info] --no-wait: follow it with"
  echo "       aws stepfunctions describe-execution --execution-arn $EXECUTION_ARN"
  exit 0
fi

echo
echo "[info] following every ${POLL_SECONDS}s (validate, launch, then the five"
echo "       pipeline steps); Ctrl-C stops watching, not the run."

STARTED_AT=$SECONDS
STATUS="RUNNING"
while [[ "$STATUS" == "RUNNING" ]]; do
  sleep "$POLL_SECONDS"
  STATUS="$(aws stepfunctions describe-execution \
    --execution-arn "$EXECUTION_ARN" --query status --output text)"
  ELAPSED=$((SECONDS - STARTED_AT))
  printf '[info] %5ds  %s\n' "$ELAPSED" "$STATUS"
  if [[ "$STATUS" == "RUNNING" && "$ELAPSED" -ge "$FOLLOW_TIMEOUT_SECONDS" ]]; then
    echo "[warn] still RUNNING after ${ELAPSED}s; giving up following. The run" >&2
    echo "       continues — check the console link above." >&2
    exit 1
  fi
done

echo
if [[ "$STATUS" == "SUCCEEDED" ]]; then
  aws stepfunctions describe-execution --execution-arn "$EXECUTION_ARN" \
    --query output --output text |
    python3 -c '
import json, sys
out = json.loads(sys.stdin.read() or "{}")
for field in ("pipeline_name", "execution_arn"):
    if out.get(field):
        print("[ok]   {:16s}: {}".format(field, out[field]))
describe = out.get("describe") or {}
for field in ("PipelineExecutionStatus", "PipelineExecutionDisplayName"):
    if describe.get(field):
        print("[ok]   {:16s}: {}".format(field, describe[field]))
'
  echo
  echo "[ok] SUCCEEDED. The model version is registered PendingManualApproval."
  echo "     Evaluate and the bias check are RECORD-ONLY — they never gate"
  echo "     registration, so read metrics.json before approving (see the runbook)."
  exit 0
fi

# FAILED / TIMED_OUT / ABORTED. `cause` is either a Lambda error payload (Validate
# rejected the config) or the failing pipeline execution's whole Describe response,
# so the useful lines are dug out of whichever it is rather than dumped.
aws stepfunctions describe-execution --execution-arn "$EXECUTION_ARN" \
  --query '{error:error,cause:cause}' --output json |
  python3 -c '
import json, sys
detail = json.loads(sys.stdin.read() or "{}")
print("[fail] error: {}".format(detail.get("error") or "(none reported)"))
cause = detail.get("cause") or ""
try:
    parsed = json.loads(cause)
except ValueError:
    if cause:
        print("[fail] cause: {}".format(cause[:600]))
else:
    # errorMessage is multi-line for a rejected config: one line per finding.
    for line in str(parsed.get("errorMessage") or "").splitlines():
        print("[fail] {}".format(line))
    for field in ("FailureReason", "PipelineExecutionStatus", "PipelineExecutionArn"):
        if parsed.get(field):
            print("[fail] {}: {}".format(field, parsed[field]))
'
echo
echo "[fail] $STATUS — see the console link above. A step failure is in the"
echo "       SageMaker Pipeline execution, not the state machine: open it in"
echo "       Studio > Pipelines, or check /aws/sagemaker/{TrainingJobs,ProcessingJobs}"
echo "       for the container output."
exit 1
