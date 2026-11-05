#!/usr/bin/env bash
# Start the cohort-label state machine for one model and follow it to a terminal
# state.
#
# Run from anywhere, with the model's cohort-label intake directory exported once
# (see the runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/patient_outcome_mortality_v2/cohort_label_intake
#   pwsa0002037_novaos_ar/docs/runbooks/cohort-label/run_pipeline.sh
#
#   --as-of <ts>    OPTIONAL. ISO-8601 UTC, e.g. 2026-09-15T00:00:00Z. The
#                   reference date the windowing resolves against; defaults to now,
#                   so pin it to make a run reproducible.
#   --run-id <id>   OPTIONAL. Must match ^r[a-z0-9]{12}$. Minted by the pipeline
#                   when omitted, which is the normal case.
#   --image-tag <t> OPTIONAL, and an escape hatch. Pins the Processing job to that
#                   ECR tag verbatim. Omitted — the normal case — stage 1 derives the
#                   tag from the form's identity.model_name, which is the tag
#                   build_and_push.sh pushed for this model.
#   --no-wait       Start it and print the execution ARN, without following.
#   ENV             environment file to read, dev | qa | prod (default: dev)
#
# The execution input is `{model_id}`, read off INTAKE_DIR — the Lambda composes the
# intake S3 prefix from it, which is why it must be the real
# <model>/cohort_label_intake directory. Upload the intake first: this starts a run
# against whatever is in S3 now.
#
# Exit code is 0 only when the execution SUCCEEDED.
set -euo pipefail

RUN_ID=""
AS_OF=""
WAIT=1
IMAGE_TAG="${IMAGE_TAG:-}"
INTAKE_DIR="${INTAKE_DIR:-}"
ENV_NAME="${ENV:-dev}"
# How often to ask, and how long before giving up following (the run itself is
# unaffected — the state machine has its own timeout_minutes).
POLL_SECONDS="${POLL_SECONDS:-30}"
FOLLOW_TIMEOUT_SECONDS="${FOLLOW_TIMEOUT_SECONDS:-86400}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir) INTAKE_DIR="$2"; shift 2 ;;
    --as-of)      AS_OF="$2";      shift 2 ;;
    --run-id)     RUN_ID="$2";     shift 2 ;;
    --image-tag)  IMAGE_TAG="$2";  shift 2 ;;
    --env)        ENV_NAME="$2";   shift 2 ;;
    --no-wait)    WAIT=0;          shift ;;
    -h|--help)    sed -n '2,28p' "$0"; exit 0 ;;
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

# Same derivation as upload_intake.sh, and for the same reason: the Lambda composes
# the intake S3 prefix from model_id, so it must be the directory's own model path.
if [[ "$(basename "$INTAKE_DIR")" != "cohort_label_intake" ]]; then
  echo "[fail] INTAKE_DIR must be a <model>/cohort_label_intake directory, so" >&2
  echo "       model_id can be read from it. Got:" >&2
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
machine = (cfg.get("state_machines") or {}).get("cohort_label") or {}
print(f'ACCOUNT="{cfg["account"]}"')
print(f'REGION="{cfg["region"]}"')
print(f'APP_PREFIX="{cfg["app_prefix"]}"')
print(f'MACHINE="{machine.get("name", "cohort-label")}"')
PY
)"

# Composed, never pasted: the name is <app_prefix>-<state machine>-<env>.
STATE_MACHINE_ARN="arn:aws:states:${REGION}:${ACCOUNT}:stateMachine:${APP_PREFIX}-${MACHINE}-${ENV_NAME}"

# `image_tag` is omitted unless asked for, rather than sent as empty: an ordinary
# run's input stays exactly what it was, and a run that pins an image is obvious in
# the execution history.
INPUT="$(python3 - "$MODEL_ID" "$RUN_ID" "$AS_OF" "$IMAGE_TAG" <<'PY'
import json, sys
model_id, run_id, as_of, image_tag = sys.argv[1:5]
payload = {"model_id": model_id}
if run_id:
    payload["run_id"] = run_id
if as_of:
    payload["as_of"] = as_of
if image_tag:
    payload["image_tag"] = image_tag
print(json.dumps(payload))
PY
)"

echo "[info] state machine : ${APP_PREFIX}-${MACHINE}-${ENV_NAME}"
echo "[info] input         : $INPUT"
[[ -n "$AS_OF" ]] ||
  echo "[warn] no --as-of: the windowing resolves against now, so this run is not reproducible."

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
echo "[info] following every ${POLL_SECONDS}s; Ctrl-C stops watching, not the run."

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
for field in ("run_id", "model_id", "compiled_intake_s3_uri", "output_s3_uri"):
    if field in out:
        print("[ok]   {:22s}: {}".format(field, out[field]))
job = (out.get("population_label_job") or {}).get("describe") or {}
if job.get("ProcessingJobName"):
    print("[ok]   {:22s}: {} ({})".format(
        "population+label job", job["ProcessingJobName"], job.get("ProcessingJobStatus")))
for warning in out.get("warnings") or []:
    print("[warn] {}: {}".format(warning.get("field"), warning.get("message")))
'
  echo
  echo "[ok] SUCCEEDED. Next: check the cohort-label report (see the runbook)."
  exit 0
fi

# FAILED / TIMED_OUT / ABORTED. `cause` is either a Lambda error payload (stage 1
# rejected the form) or the failing job's whole Describe response, so the useful
# lines are dug out of whichever it is rather than dumped.
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
    # errorMessage is multi-line for a rejected form: one line per finding.
    for line in str(parsed.get("errorMessage") or "").splitlines():
        print("[fail] {}".format(line))
    for field in ("FailureReason", "ProcessingJobName", "ProcessingJobStatus"):
        if parsed.get(field):
            print("[fail] {}: {}".format(field, parsed[field]))
'
echo
echo "[fail] $STATUS — see the console link above, or the job log group"
echo "       /aws/sagemaker/ProcessingJobs for the container output."
exit 1
