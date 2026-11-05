#!/usr/bin/env bash
# Build the model-ready-dataset Processing image and push it to ECR, baking in the
# strategy distribution and optionally one model's form-local transformations.
#
# Run from anywhere, with the model's MRD intake directory exported once (see the
# runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/patient_outcome_mortality_v2/model_ready_dataset
#   pwsa0002037_novaos_ar/docs/runbooks/model-ready-dataset/build_and_push.sh
#
# ONE image serves all three stages (pit-join, split, validate-and-write); the state
# machine selects the script per stage with ContainerEntrypoint.
#
# A strategy resolves from exactly two places: the ar_transformations distribution,
# installed from the JFrog PyPI repo below, and the model's own
# local_transformations/ tree. No clone of the transformations repo is involved —
# the form dispatches to ar_transformations.*, which the published sdist ships.
#
# INTAKE_DIR decides BOTH what is baked in and what the image is called. Set, the
# form's local_transformations/ tree is copied to /novos/local_transformations/ so a
# module the form names as local_transformations.<x> imports at job start, the form's
# compute.local_requirements are installed, and the tag carries the model so this
# image is the one that model's runs pull. Unset, the image is built with an EMPTY
# local_transformations tier under the bare prefix tag — a distribution-only build
# that no run pulls.
#
# A run NEVER builds an image — that would bypass the ECR scan gate.
#
# Everything that varies by environment (account, region, repo, tag) comes from
# cdk_pipeline/environments/<env>.json, never from this script.
set -euo pipefail

ENV_NAME="${ENV:-dev}"
INTAKE_DIR="${INTAKE_DIR:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir) INTAKE_DIR="$2"; shift 2 ;;
    --env)        ENV_NAME="$2";   shift 2 ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "[fail] unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Resolved BEFORE the cd below, because a relative path is relative to where the
# operator ran this from. The model is the path above model_ready_dataset/ — one or
# two segments depending on the model — so the models repo root comes from git
# rather than from counting parents.
MODELS_REPO=""
MODEL_ID=""
MODEL_NAME=""
if [[ -n "$INTAKE_DIR" ]]; then
  [[ -d "$INTAKE_DIR" ]] ||
    { echo "[fail] INTAKE_DIR is not a directory: $INTAKE_DIR" >&2; exit 1; }
  INTAKE_DIR="$(cd "$INTAKE_DIR" && pwd)"
  [[ "$(basename "$INTAKE_DIR")" == "model_ready_dataset" ]] ||
    { echo "[fail] INTAKE_DIR must be a <model>/model_ready_dataset directory. Got: $INTAKE_DIR" >&2; exit 1; }
  MODELS_REPO="$(git -C "$INTAKE_DIR" rev-parse --show-toplevel)" ||
    { echo "[fail] $INTAKE_DIR is not inside a clone of the sample-models repo." >&2; exit 1; }
  # MODEL_ID locates things on disk — the models-repo-relative path this form sits
  # at. It does NOT name the image: the tag comes from the form (see below), so the
  # image a run pulls does not depend on where anyone cloned this repo.
  MODEL_ID="$(dirname "${INTAKE_DIR#"$MODELS_REPO"/}")"
  MODEL_NAME="$(python3 - "$INTAKE_DIR/model-ready-dataset-intake-form.yaml" <<'PY'
import sys, yaml
form = yaml.safe_load(open(sys.argv[1])) or {}
name = str((form.get("identity") or {}).get("model_name") or "").strip()
if not name:
    sys.exit("identity.model_name is missing or empty")
print(name)
PY
)" || { echo "[fail] could not read identity.model_name from" >&2
        echo "       $INTAKE_DIR/model-ready-dataset-intake-form.yaml" >&2; exit 1; }
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="cdk_pipeline/environments/${ENV_NAME}.json"
[[ -f "$ENV_FILE" ]] || { echo "[fail] no such environment file: $ENV_FILE" >&2; exit 1; }

eval "$(python3 - "$ENV_FILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
print(f'ACCOUNT="{cfg["account"]}"')
print(f'REGION="{cfg["region"]}"')
print(f'ECR_REPO="{cfg["ecr_repo_name"]}"')
PY
)"

REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
JOB_TAG_PREFIX="${JOB_TAG_PREFIX:-novos-ar-job-model-ready-dataset}"
BASE_TAG="${BASE_TAG:-novos-ar-base-cpu}"
BASE_IMAGE="${REGISTRY}/${ECR_REPO}:${BASE_TAG}"

# ── the tag carries the model ────────────────────────────────────────────────
# This image bakes ONE model's local_transformations tree and its
# local_requirements, so it cannot serve another model and must not share a tag
# with one: with a single fixed tag, the last team to push owns it and every other
# team's run pulls an image without its strategies. All three stages still share
# this one image — the tag separates models, not stages.
#
# src/common/image_tags composes the tag, and the stage-1 Lambda composes the tag it
# writes into the compiled intake from that same function and the same field —
# identity.model_name, read from this same form — so what is pushed here is what the
# run pulls. Never re-implement it in bash.
IMAGE_TAG="${IMAGE_TAG:-}"
if [[ -n "$IMAGE_TAG" ]]; then
  echo "[warn] IMAGE_TAG is set: pushing '$IMAGE_TAG' verbatim rather than the tag"
  echo "[warn] derived from the model. A run only pulls this image if it was started"
  echo "[warn] with --image-tag '$IMAGE_TAG'."
elif [[ -n "$MODEL_NAME" ]]; then
  IMAGE_TAG="$(PYTHONPATH="$REPO_ROOT" python3 - \
      "$JOB_TAG_PREFIX" "$MODEL_NAME" <<'PY'
import sys
from src.common.image_tags import job_image_tag
print(job_image_tag(sys.argv[1], sys.argv[2]))
PY
)" || { echo "[fail] could not compose an image tag for model '$MODEL_NAME'." >&2
        exit 1; }
  echo "[info] model         : $MODEL_NAME"
else
  # No form, no model: the distribution-only image. No run pulls this tag — the
  # compiler always names a per-model one — so this builds the warm base of the
  # layer cache and proves the Dockerfile still builds.
  IMAGE_TAG="$JOB_TAG_PREFIX"
  echo "[warn] no INTAKE_DIR: building the distribution-only image '$IMAGE_TAG'."
  echo "[warn] No model's run pulls this tag. Pass --intake-dir to build the image a"
  echo "[warn] run will actually use."
fi
IMAGE="${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"

# ── stage the build context ──────────────────────────────────────────────────
# Docker can only read from the build context (the repo root), so everything from
# outside is COPIED in. All three dirs are tracked with a .gitkeep and their
# contents gitignored, so a build with no model still succeeds and nothing of a
# team's ever gets committed by accident.
#
# The requirements file is staged SEPARATELY from the tree because the form's
# compute.local_requirements is repo-relative, not form-relative: it is a shared
# requirements.txt a form points at, so it can legitimately sit above the intake
# directory and must not collide with anything inside the tree.
STAGING="src/images/job-model-ready-dataset/local_transformations"
REQS_STAGING="src/images/job-model-ready-dataset/local_requirements"
ENGINE_STAGING="src/images/job-model-ready-dataset/transformations"
for dir in "$STAGING" "$REQS_STAGING" "$ENGINE_STAGING"; do
  rm -rf "$dir"
  mkdir -p "$dir"
  touch "$dir/.gitkeep"
done
# The Dockerfile COPYs this unconditionally, so it must exist even with no form.
: > "$REQS_STAGING/requirements.txt"

# ── stage the transformations distribution ───────────────────────────────────
# The sdist is downloaded HERE and installed by the Dockerfile from the staged
# tree.
TRANSFORMATIONS_DIST="pwsa0002037-novaos-ar-transformations"
TRANSFORMATIONS_VERSION="${TRANSFORMATIONS_VERSION:-0.1.0}"
JFROG_SECRET_ID="${JFROG_SECRET_ID:-jfrog-token}"
JFROG_PYPI_REPO="${JFROG_PYPI_REPO:-f1-imu-im-pwsa0002058-pypi}"

# The sdist filename normalises the distribution's hyphens to underscores.
SDIST_NAME="${TRANSFORMATIONS_DIST//-/_}-${TRANSFORMATIONS_VERSION}.tar.gz"
ARTIFACT_URL="https://artifactory.f1.novartis.net/artifactory/${JFROG_PYPI_REPO}/${TRANSFORMATIONS_DIST}/${TRANSFORMATIONS_VERSION}/${SDIST_NAME}"

echo "[info] fetching ${TRANSFORMATIONS_DIST} ${TRANSFORMATIONS_VERSION} from ${JFROG_PYPI_REPO}"
read -r JFROG_USER JFROG_TOKEN < <(
  aws secretsmanager get-secret-value --secret-id "$JFROG_SECRET_ID" \
    --query SecretString --output text | jq -r 'to_entries[0] | "\(.key) \(.value)"'
) || {
  echo "[fail] could not read a user/token pair from secret ${JFROG_SECRET_ID}." >&2
  exit 1
}

SDIST="$(mktemp)"
trap 'rm -f "$SDIST"' EXIT
# -u rather than credentials in the URL: curl echoes the URL it tried on failure.
if ! curl -sS -f -u "${JFROG_USER}:${JFROG_TOKEN}" -o "$SDIST" "$ARTIFACT_URL"; then
  echo "[fail] could not download ${SDIST_NAME}." >&2
  echo "       Check that version is published and that ${JFROG_SECRET_ID} can read" >&2
  echo "       ${JFROG_PYPI_REPO}." >&2
  exit 1
fi
unset JFROG_TOKEN

# Unpacked rather than staged as the tarball, so the Dockerfile keeps installing a
# directory that has a pyproject.toml — the one shape it knows.
tar xzf "$SDIST" -C "$ENGINE_STAGING" --strip-components=1
[[ -f "$ENGINE_STAGING/pyproject.toml" ]] ||
  { echo "[fail] ${SDIST_NAME} unpacked with no pyproject.toml; is that an sdist?" >&2; exit 1; }
echo "[info] staged ${SDIST_NAME} ($(wc -c <"$SDIST") bytes)"

# ── stage the form-local tier ────────────────────────────────────────────────
if [[ -n "$INTAKE_DIR" ]]; then
  echo "[info] model           : $MODEL_NAME"
  echo "[info] intake directory: $INTAKE_DIR"

  if [[ -d "$INTAKE_DIR/local_transformations" ]]; then
    echo "[info] baking in $INTAKE_DIR/local_transformations"
    cp -R "$INTAKE_DIR/local_transformations/." "$STAGING/"
    # A local validate_intake.py run leaves bytecode behind; shipping it would
    # bake a stale compile of the team's modules into the image.
    find "$STAGING" -name '__pycache__' -type d -prune -exec rm -rf {} +
  else
    echo "[info] no local_transformations/ in $INTAKE_DIR — distribution only"
  fi

  # compute.local_requirements is relative to the MODELS REPO root, not the form.
  LOCAL_REQS="$(python3 - "$INTAKE_DIR/model-ready-dataset-intake-form.yaml" <<'PY'
import sys, yaml
form = yaml.safe_load(open(sys.argv[1])) or {}
print(str((form.get("compute") or {}).get("local_requirements") or "").strip())
PY
)"
  if [[ -n "$LOCAL_REQS" ]]; then
    REQS_PATH="${MODELS_REPO}/${LOCAL_REQS}"
    [[ -f "$REQS_PATH" ]] || { echo "[fail] compute.local_requirements is '$LOCAL_REQS' but $REQS_PATH does not exist." >&2; exit 1; }
    echo "[info] installing compute.local_requirements: $LOCAL_REQS"
    cp "$REQS_PATH" "$REQS_STAGING/requirements.txt"
  fi

  echo "[info] local packages staged: $(grep -chv '^\s*\(#\|$\)' "$REQS_STAGING/requirements.txt")"
else
  echo "[info] no INTAKE_DIR: building with an empty local_transformations tier"
fi

# ── build ────────────────────────────────────────────────────────────────────
# ValidateVPCOnlyModeImage reads the FROM line BEFORE ARG substitution, so a
# Dockerfile whose FROM is ${BASE_CPU_IMAGE} is rejected as "not an ECR Image".
# Build from a copy that differs only in that line.
MATERIALISED="$(mktemp)"
trap 'rm -f "$SDIST" "$MATERIALISED"' EXIT
sed "s|^FROM .*|FROM ${BASE_IMAGE}|" src/images/job-model-ready-dataset/Dockerfile > "$MATERIALISED"

# --network sagemaker is mandatory inside SageMaker Studio and harmless elsewhere
# when that network exists; drop it with DOCKER_NETWORK="" on a plain host.
DOCKER_NETWORK="${DOCKER_NETWORK-sagemaker}"
NETWORK_ARG=()
[[ -n "$DOCKER_NETWORK" ]] && NETWORK_ARG=(--network "$DOCKER_NETWORK")

BUILD_PROXY="${HTTPS_PROXY:-${HTTP_PROXY:-}}"
NO_PROXY_HOSTS="${NO_PROXY:-169.254.169.254,localhost,novartis.net,*.novartis.net}"
PROXY_ARGS=()
if [[ -n "$BUILD_PROXY" ]]; then
  PROXY_ARGS=(
    --build-arg HTTP_PROXY="$BUILD_PROXY" --build-arg HTTPS_PROXY="$BUILD_PROXY"
    --build-arg NO_PROXY="$NO_PROXY_HOSTS"
  )
  echo "[info] build-time proxy: $BUILD_PROXY"
else
  echo "[warn] no HTTP_PROXY/HTTPS_PROXY in the environment; pip may not reach pypi."
fi


echo "[info] logging in to $REGISTRY"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "[info] building $IMAGE"
docker build "${NETWORK_ARG[@]}" "${PROXY_ARGS[@]}" -f "$MATERIALISED" -t "$IMAGE" .

echo "[info] pushing $IMAGE"
docker push "$IMAGE"

echo
echo "[ok] pushed $IMAGE"
echo "[ok] transformations installed: ${TRANSFORMATIONS_DIST} ${TRANSFORMATIONS_VERSION}"
if [[ -n "$INTAKE_DIR" ]]; then
  echo "[ok] baked in ${MODEL_ID}/model_ready_dataset"
  # The run resolves this same tag from the same identity.model_name in the same form,
  # so a run that reports a different one read a different form than was built here.
  echo "[ok] tag ${IMAGE_TAG} — run_pipeline.sh for ${MODEL_NAME} pulls it"
fi
