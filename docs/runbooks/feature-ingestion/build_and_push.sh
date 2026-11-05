#!/usr/bin/env bash
# Build the feature-ingestion Processing image and push it to ECR, optionally
# baking in one feature group's form-local transformation tier.
#
# Run from anywhere, with your feature group's intake directory exported once (see
# the runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/...
#   pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/build_and_push.sh
#
# A feature's transformation resolves from exactly two places: the
# ar_transformations distribution, installed from the JFrog PyPI repo below, and
# the feature group's own local_transformations/ tree.
#
# INTAKE_DIR decides BOTH what is baked in and what the image is called. Set, its
# local_transformations/ is copied to /novos/local_transformations/ so a module a
# form names as local_transformations.<x> imports at job start, the form's
# compute.processing.local_requirements are installed, and the image is TAGGED FOR
# THAT FEATURE GROUP — <prefix>-<identity.feature_group_name>, which is the tag the
# group's run pulls. Unset, the image is built with an empty local tier under the
# bare prefix, and no run pulls it.
#
# So a feature group's image is per-group, always: build it once per group, and
# again whenever that group's local_transformations or local_requirements change.
#
# TRANSFORMATIONS_DIR is OPTIONAL, and works the same way. Set, the distribution
# is built as an sdist from that local checkout instead of downloaded from JFrog,
# so a transformation merged but not yet published can be run. The image then
# carries that working tree and is NOT reproducible from a version number.
#
# Everything that varies by environment (account, region, repo, tag) comes from
# cdk_pipeline/environments/<env>.json, never from this script.
set -euo pipefail

ENV_NAME="${ENV:-dev}"
INTAKE_DIR="${INTAKE_DIR:-}"
TRANSFORMATIONS_DIR="${TRANSFORMATIONS_DIR:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir)           INTAKE_DIR="$2";           shift 2 ;;
    --transformations-dir)  TRANSFORMATIONS_DIR="$2";  shift 2 ;;
    --env)                  ENV_NAME="$2";             shift 2 ;;
    -h|--help)       sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "[fail] unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Both resolved BEFORE the cd below, because a relative path is relative to where
# the operator ran this from.
if [[ -n "$INTAKE_DIR" ]]; then
  [[ -d "$INTAKE_DIR" ]] ||
    { echo "[fail] INTAKE_DIR is not a directory: $INTAKE_DIR" >&2; exit 1; }
  INTAKE_DIR="$(cd "$INTAKE_DIR" && pwd)"
fi
if [[ -n "$TRANSFORMATIONS_DIR" ]]; then
  [[ -d "$TRANSFORMATIONS_DIR" ]] ||
    { echo "[fail] TRANSFORMATIONS_DIR is not a directory: $TRANSFORMATIONS_DIR" >&2
      exit 1; }
  TRANSFORMATIONS_DIR="$(cd "$TRANSFORMATIONS_DIR" && pwd)"
  [[ -f "$TRANSFORMATIONS_DIR/pyproject.toml" ]] ||
    { echo "[fail] no pyproject.toml in $TRANSFORMATIONS_DIR — it must be the root" >&2
      echo "       of the ar_transformations checkout, not a subdirectory." >&2
      exit 1; }
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
JOB_TAG_PREFIX="${JOB_TAG_PREFIX:-novos-ar-job-feature-ingestion}"
BASE_TAG="${BASE_TAG:-novos-ar-base-cpu}"
BASE_IMAGE="${REGISTRY}/${ECR_REPO}:${BASE_TAG}"

# ── the tag carries the feature group ────────────────────────────────────────
# This image bakes ONE group's local_transformations tree and its
# local_requirements, so it cannot serve another group and must not share a tag
# with one: with a single fixed tag, the last team to push owns it and every other
# team's run pulls an image without its transformations.
#
# src/common/image_tags composes the tag, and the compiler Lambda composes the tag
# it writes into the compiled intake from that same function and the same field —
# so what is pushed here is what the run pulls. Never re-implement it in bash.
IMAGE_TAG="${IMAGE_TAG:-}"
if [[ -n "$IMAGE_TAG" ]]; then
  echo "[warn] IMAGE_TAG is set: pushing '$IMAGE_TAG' verbatim rather than the tag"
  echo "[warn] derived from the feature group. A run only pulls this image if it was"
  echo "[warn] started with --image-tag '$IMAGE_TAG'."
elif [[ -n "$INTAKE_DIR" ]]; then
  FEATURE_GROUP_NAME="$(python3 - "$INTAKE_DIR/feature-intake-form.yaml" <<'PY'
import sys, yaml
form = yaml.safe_load(open(sys.argv[1])) or {}
name = str((form.get("identity") or {}).get("feature_group_name") or "").strip()
if not name:
    sys.exit("identity.feature_group_name is missing or empty")
print(name)
PY
)" || { echo "[fail] could not read identity.feature_group_name from" >&2
        echo "       $INTAKE_DIR/feature-intake-form.yaml" >&2; exit 1; }
  IMAGE_TAG="$(PYTHONPATH="$REPO_ROOT" python3 - \
      "$JOB_TAG_PREFIX" "$FEATURE_GROUP_NAME" <<'PY'
import sys
from src.common.image_tags import job_image_tag
print(job_image_tag(sys.argv[1], sys.argv[2]))
PY
)" || { echo "[fail] could not compose an image tag for feature group" >&2
        echo "       '$FEATURE_GROUP_NAME'." >&2; exit 1; }
  echo "[info] feature group  : $FEATURE_GROUP_NAME"
else
  # No form, no group: the distribution-only image. No run pulls this tag — the
  # compiler always names a per-group one — so this builds the warm base of the
  # layer cache and proves the Dockerfile still builds.
  IMAGE_TAG="$JOB_TAG_PREFIX"
  echo "[warn] no INTAKE_DIR: building the distribution-only image '$IMAGE_TAG'."
  echo "[warn] No feature group's run pulls this tag. Pass --intake-dir to build the"
  echo "[warn] image a run will actually use."
fi
IMAGE="${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"

# ── stage the transformations distribution into the build context ────────────
# The sdist is downloaded HERE and installed by the Dockerfile from the staged
# file. The credential therefore never becomes a build arg and never reaches a
# layer. It is read from Secrets Manager by NAME, not by ARN, so the lookup
# resolves in whichever account and Region the caller is authenticated to —
# this script hardcodes nothing environment-specific.
ENGINE_STAGING="src/images/job-feature-ingestion/transformations"
TRANSFORMATIONS_DIST="pwsa0002037-novaos-ar-transformations"
TRANSFORMATIONS_VERSION="${TRANSFORMATIONS_VERSION:-0.1.0}"
JFROG_SECRET_ID="${JFROG_SECRET_ID:-jfrog-token}"
JFROG_PYPI_REPO="${JFROG_PYPI_REPO:-f1-imu-im-pwsa0002058-pypi}"

rm -rf "$ENGINE_STAGING"
mkdir -p "$ENGINE_STAGING"
touch "$ENGINE_STAGING/.gitkeep"

# The sdist filename normalises the distribution's hyphens to underscores.
SDIST_NAME="${TRANSFORMATIONS_DIST//-/_}-${TRANSFORMATIONS_VERSION}.tar.gz"
ARTIFACT_URL="https://artifactory.f1.novartis.net/artifactory/${JFROG_PYPI_REPO}/${TRANSFORMATIONS_DIST}/${TRANSFORMATIONS_VERSION}/${SDIST_NAME}"

if [[ -n "$TRANSFORMATIONS_DIR" ]]; then
  echo "[warn] building the distribution from a LOCAL CHECKOUT, not ${JFROG_PYPI_REPO}:"
  echo "[warn]   $TRANSFORMATIONS_DIR"
  echo "[warn] The image will carry that working tree, uncommitted edits included,"
  echo "[warn] and is NOT reproducible from a version number. Do not promote it."
  python3 -m build --sdist --no-isolation \
    --outdir "$ENGINE_STAGING" "$TRANSFORMATIONS_DIR" >/dev/null || {
    echo "[fail] could not build an sdist from $TRANSFORMATIONS_DIR." >&2
    echo "       Re-run without redirecting stdout to see the backend's error," >&2
    echo "       and check that 'python3 -m build' is installed." >&2
    exit 1
  }
  mapfile -t STAGED < <(find "$ENGINE_STAGING" -name '*.tar.gz' -type f)
  if [[ "${#STAGED[@]}" -ne 1 ]]; then
    echo "[fail] expected one sdist in $ENGINE_STAGING, found ${#STAGED[@]}." >&2
    exit 1
  fi
  SDIST_NAME="$(basename "${STAGED[0]}")"
else
  echo "[info] fetching ${TRANSFORMATIONS_DIST} ${TRANSFORMATIONS_VERSION} from ${JFROG_PYPI_REPO}"
  read -r JFROG_USER JFROG_TOKEN < <(
    aws secretsmanager get-secret-value --secret-id "$JFROG_SECRET_ID" \
      --query SecretString --output text | jq -r 'to_entries[0] | "\(.key) \(.value)"'
  ) || {
    echo "[fail] could not read a user/token pair from secret ${JFROG_SECRET_ID}." >&2
    exit 1
  }

  # -u rather than credentials in the URL: curl echoes the URL it tried on failure.
  if ! curl -sS -f -u "${JFROG_USER}:${JFROG_TOKEN}" \
       -o "${ENGINE_STAGING}/${SDIST_NAME}" "$ARTIFACT_URL"; then
    echo "[fail] could not download ${SDIST_NAME}." >&2
    echo "       Check that version is published and that ${JFROG_SECRET_ID} can read" >&2
    echo "       ${JFROG_PYPI_REPO}." >&2
    exit 1
  fi
  unset JFROG_TOKEN
fi
echo "[info] staged ${SDIST_NAME} ($(wc -c <"${ENGINE_STAGING}/${SDIST_NAME}") bytes)"

# ── stage the feature group's form-local tier ────────────────────────────────
# Docker can only read from the build context, so the tree is COPIED out of the
# separate models repo
STAGING="src/images/job-feature-ingestion/local_transformations"
REQS_STAGING="src/images/job-feature-ingestion/local_requirements"
for dir in "$STAGING" "$REQS_STAGING"; do
  rm -rf "$dir"
  mkdir -p "$dir"
  touch "$dir/.gitkeep"
done
# The Dockerfile COPYs this unconditionally, so it must exist even with no form.
: > "$REQS_STAGING/requirements.txt"

if [[ -n "$INTAKE_DIR" ]]; then
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

  LOCAL_REQS="$(python3 - "$INTAKE_DIR/feature-intake-form.yaml" <<'PY'
import sys, yaml
form = yaml.safe_load(open(sys.argv[1])) or {}
processing = (form.get("compute") or {}).get("processing") or {}
print(str(processing.get("local_requirements") or "").strip())
PY
)"
  if [[ -n "$LOCAL_REQS" ]]; then
    # That path is relative to the MODELS REPO root, not the form: it is a shared
    # requirements.txt a form points at, so it can sit above the intake directory.
    # The root is <brand>/<model>/feature_intake/<group> up four.
    MODELS_ROOT="$(cd "$INTAKE_DIR/../../../.." && pwd)"
    REQS_PATH="${MODELS_ROOT}/${LOCAL_REQS}"
    [[ -f "$REQS_PATH" ]] ||
      { echo "[fail] compute.processing.local_requirements is '$LOCAL_REQS' but $REQS_PATH does not exist." >&2; exit 1; }
    echo "[info] installing compute.processing.local_requirements: $LOCAL_REQS"
    cp "$REQS_PATH" "$REQS_STAGING/requirements.txt"
  fi
  echo "[info] local packages staged: $(grep -chv '^\s*\(#\|$\)' "$REQS_STAGING/requirements.txt")"
else
  echo "[info] no INTAKE_DIR: empty local_transformations tier"
fi

# ── build ────────────────────────────────────────────────────────────────────
# ValidateVPCOnlyModeImage reads the FROM line BEFORE ARG substitution, so a
# Dockerfile whose FROM is ${BASE_CPU_IMAGE} is rejected as "not an ECR Image".
# Build from a copy that differs only in that line.
MATERIALISED="$(mktemp)"
trap 'rm -f "$MATERIALISED"' EXIT
sed "s|^FROM .*|FROM ${BASE_IMAGE}|" src/images/job-feature-ingestion/Dockerfile > "$MATERIALISED"

# --network sagemaker is mandatory inside SageMaker Studio and harmless elsewhere
# when that network exists; drop it with DOCKER_NETWORK="" on a plain host.
DOCKER_NETWORK="${DOCKER_NETWORK-sagemaker}"
NETWORK_ARG=()
[[ -n "$DOCKER_NETWORK" ]] && NETWORK_ARG=(--network "$DOCKER_NETWORK")

BUILD_PROXY="${HTTPS_PROXY:-${HTTP_PROXY:-}}"
NO_PROXY_HOSTS="${NO_PROXY:-169.254.169.254,localhost,novartis.net,*.novartis.net,*.privatelink.snowflakecomputing.com}"
PROXY_ARGS=()
if [[ -n "$BUILD_PROXY" ]]; then
  PROXY_ARGS=(
    --build-arg HTTP_PROXY="$BUILD_PROXY" --build-arg HTTPS_PROXY="$BUILD_PROXY"
    --build-arg http_proxy="$BUILD_PROXY" --build-arg https_proxy="$BUILD_PROXY"
    --build-arg NO_PROXY="$NO_PROXY_HOSTS" --build-arg no_proxy="$NO_PROXY_HOSTS"
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
  echo "[ok] baked in ${INTAKE_DIR}/local_transformations"
fi
if [[ -n "${FEATURE_GROUP_NAME:-}" ]]; then
  # The run resolves this same tag from the same field, so a run that reports a
  # different one was started against a different form than was built here.
  echo "[ok] tag ${IMAGE_TAG} — run_pipeline.sh for ${FEATURE_GROUP_NAME} pulls it"
fi
