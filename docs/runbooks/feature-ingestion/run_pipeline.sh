#!/usr/bin/env bash
# Start the feature-onboarding state machine for one feature group and follow it
# to a terminal state.
#
# Run from anywhere, with your feature group's intake directory exported once (see
# the runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/...
#   pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/run_pipeline.sh
#
#   --run-id <id>   OPTIONAL. Must match ^r[a-z0-9]{12}$. Minted by the pipeline
#                   when omitted, which is the normal case.
#   --recreate      DESTRUCTIVE. Lets stage 2 DELETE the feature group and its
#                   records and create it again, when the intake conflicts with
#                   the group that already exists. Asks for confirmation. Does
#                   nothing when nothing conflicts, so it is not a way to force
#                   a reset.
#   --image-tag <t> OPTIONAL, and an escape hatch. Pins the ingest job to that ECR
#                   tag verbatim. Omitted — the normal case — stage 1 derives the
#                   tag from the feature group, which is the tag build_and_push.sh
#                   pushed for it.
#   --no-wait       Start it and print the execution ARN, without following.
#   ENV             environment file to read, dev | qa | prod (default: dev)
#
# The execution input is `{model_id, feature_group_name}`, both read off
# INTAKE_DIR — the state machine composes the S3 prefix from them, which is why it
# must be the real <brand>/<model>/feature_intake/<group> directory. Upload the
# intake first: this starts a run against whatever is in S3 now.
#
# Exit code is 0 only when the execution SUCCEEDED.
set -euo pipefail

RUN_ID=""
WAIT=1
RECREATE=0
IMAGE_TAG="${IMAGE_TAG:-}"
INTAKE_DIR="${INTAKE_DIR:-}"
ENV_NAME="${ENV:-dev}"
# How often to ask, and how long before giving up following (the run itself is
# unaffected — the state machine has its own timeout_minutes).
POLL_SECONDS="${POLL_SECONDS:-15}"
FOLLOW_TIMEOUT_SECONDS="${FOLLOW_TIMEOUT_SECONDS:-86400}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir) INTAKE_DIR="$2"; shift 2 ;;
    --run-id)     RUN_ID="$2";     shift 2 ;;
    --image-tag)  IMAGE_TAG="$2";  shift 2 ;;
    --env)        ENV_NAME="$2";   shift 2 ;;
    --no-wait)    WAIT=0;          shift ;;
    --recreate)   RECREATE=1;      shift ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
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

if [[ -n "$RUN_ID" && ! "$RUN_ID" =~ ^r[a-z0-9]{12}$ ]]; then
  echo "[fail] --run-id must match ^r[a-z0-9]{12}\$ — got '$RUN_ID'." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_FILE="${REPO_ROOT}/cdk_pipeline/environments/${ENV_NAME}.json"
[[ -f "$ENV_FILE" ]] || { echo "[fail] no such environment file: $ENV_FILE" >&2; exit 1; }

# Same derivation as upload_intake.sh, and for the same reason: the state machine
# composes the intake S3 prefix from these two, so they must be the directory's
# own <brand>/<model> and <group>.
FEATURE_GROUP_NAME="$(basename "$INTAKE_DIR")"
if [[ "$(basename "$(dirname "$INTAKE_DIR")")" != "feature_intake" ]]; then
  echo "[fail] INTAKE_DIR must be a <brand>/<model>/feature_intake/<group> directory," >&2
  echo "       so model_id and feature_group_name can be read from it. Got:" >&2
  echo "         $INTAKE_DIR" >&2
  exit 1
fi
MODEL_DIR="$(dirname "$(dirname "$INTAKE_DIR")")"
MODEL_ID="$(basename "$(dirname "$MODEL_DIR")")/$(basename "$MODEL_DIR")"

if [[ "$RECREATE" -eq 1 ]]; then
  cat >&2 <<WARNING

[warn] --recreate lets stage 2 DELETE this feature group and CREATE it again,
       unconditionally, even if the uploaded intake matches the group that
       already exists:

         model_id           : $MODEL_ID
         feature_group_name : $FEATURE_GROUP_NAME

WARNING
  [[ -t 0 ]] || {
    echo "[fail] --recreate needs an interactive terminal to confirm." >&2; exit 1; }
  read -r -p "Type the feature group name to confirm: " CONFIRMED
  [[ "$CONFIRMED" == "$FEATURE_GROUP_NAME" ]] || {
    echo "[fail] not confirmed — nothing was started." >&2; exit 1; }
fi

eval "$(python3 - "$ENV_FILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
machine = (cfg.get("state_machines") or {}).get("feature_onboarding") or {}
print(f'ACCOUNT="{cfg["account"]}"')
print(f'REGION="{cfg["region"]}"')
print(f'APP_PREFIX="{cfg["app_prefix"]}"')
print(f'MACHINE="{machine.get("name", "feature-onboarding")}"')
PY
)"

# Composed, never pasted: the name is <app_prefix>-<state machine>-<env>.
STATE_MACHINE_ARN="arn:aws:states:${REGION}:${ACCOUNT}:stateMachine:${APP_PREFIX}-${MACHINE}-${ENV_NAME}"

# `recreate` and `image_tag` are omitted unless asked for, rather than sent as false
# / empty: stage 1 echoes bool(event.get("recreate")) either way, so an ordinary
# run's input stays exactly what it was, and a run that carries either is obvious in
# the execution history.
INPUT="$(python3 - "$MODEL_ID" "$FEATURE_GROUP_NAME" "$RUN_ID" "$RECREATE" "$IMAGE_TAG" <<'PY'
import json, sys
model_id, feature_group_name, run_id, recreate, image_tag = sys.argv[1:6]
payload = {"model_id": model_id, "feature_group_name": feature_group_name}
if run_id:
    payload["run_id"] = run_id
if recreate == "1":
    payload["recreate"] = True
if image_tag:
    payload["image_tag"] = image_tag
print(json.dumps(payload))
PY
)"

echo "[info] state machine : ${APP_PREFIX}-${MACHINE}-${ENV_NAME}"
echo "[info] input         : $INPUT"

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
echo "[info] following every ${POLL_SECONDS}s (expect 6-10 minutes); Ctrl-C stops"
echo "       watching, not the run."

STARTED_AT=$SECONDS
STATUS="RUNNING"
while [[ "$STATUS" == "RUNNING" ]]; do
  sleep "$POLL_SECONDS"
  STATUS="$(aws stepfunctions describe-execution \
    --execution-arn "$EXECUTION_ARN" --query status --output text)"
  ELAPSED=$((SECONDS - STARTED_AT))
  printf '[info] %4ds  %s\n' "$ELAPSED" "$STATUS"
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
for field in ("run_id", "feature_group_name", "feature_count", "compiled_intake_s3_uri"):
    if field in out:
        print("[ok]   {:22s}: {}".format(field, out[field]))
job = (out.get("ingest_job") or {}).get("describe") or {}
if job.get("ProcessingJobName"):
    print("[ok]   {:22s}: {} ({})".format(
        "ingest job", job["ProcessingJobName"], job.get("ProcessingJobStatus")))
'
  echo
  echo "[ok] SUCCEEDED. Next: check the ingest report (see the runbook)."
  exit 0
fi

# FAILED / TIMED_OUT / ABORTED. `cause` is usually the failing job's whole
# Describe response, so the useful line is dug out rather than dumped.
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
    for field in ("FailureReason", "ProcessingJobName", "ProcessingJobStatus"):
        if parsed.get(field):
            print("[fail] {}: {}".format(field, parsed[field]))
'
echo
echo "[fail] $STATUS — see the console link above, or the job log group"
echo "       /aws/sagemaker/ProcessingJobs for the container output."
exit 1
