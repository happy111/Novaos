#!/usr/bin/env bash
# TEMPORARY script — builds + pushes the GENERIC (no model code) training-pipeline
# images, under the platform's fixed job-image vocabulary:
#   preprocessing    -> novos-ar-base-cpu-job-preprocessing
#   training (cpu)   -> novos-ar-base-cpu-job-training
#   training (gpu)   -> novos-ar-base-gpu-job-training
#   evaluation (cpu) -> novos-ar-base-cpu-job-evaluation
#   evaluation (gpu) -> novos-ar-base-gpu-job-evaluation
#
# WHY THIS EXISTS. A model-owned Dockerfile (compute.<step>.dockerfile) builds
# FROM one of these five generic images instead of the bare account base — it
# already carries the stage's framework deps (torch/scikit-learn/pandas/pyarrow/
# pyyaml as applicable), so a model-owned recipe only needs to add its launcher +
# code + any EXTRA deps (via local_requirements.txt), not redeclare the whole
# framework. This script builds and pushes those five images.
#
# THIS IS TEMPORARY. It proves the five images build and push correctly under
# the platform's naming; the durable version of this is wiring these five stages
# into cdk_pipeline/config-src-images.yaml + build-src-images.sh/push-src-images.sh
# alongside job-feature-ingestion/job-cohort-label/job-model-ready-dataset — a
# separate follow-up. job-training (also in that yaml) is an unrelated
# Lambda-base image, not one of these five stages.
#
# WHAT IT BUILDS. Same five Dockerfiles src/images/build_and_push.sh uses
# (job-preprocessing/, job-training-cpu/, job-training-gpu/, job-evaluation-cpu/,
# job-evaluation-gpu/), each staged with an EMPTY code/ (no model), the real
# ar_transformations sdist (the Dockerfile hard-fails without SOME sdist staged
# — these images still need the package importable), and an empty
# requirements.txt/launcher matching build_and_push.sh's own defaults.
#
# DO NOT run this at the same time as src/images/build_and_push.sh — both stage
# into the same src/images/job-<stage>/{code,transformations}/ directories, so a
# concurrent run of the other script would race and corrupt the build context.
#
# USAGE (run from anywhere — resolves the repo root itself):
#   bash docs/runbooks/model-training/build_and_push_generic.sh              # all five
#   bash docs/runbooks/model-training/build_and_push_generic.sh preprocessing training-cpu   # a subset (stage words)
#   bash docs/runbooks/model-training/build_and_push_generic.sh --no-push    # local smoke build, no ECR
#   bash docs/runbooks/model-training/build_and_push_generic.sh --build-network sagemaker   # inside Studio
#
# Overridable by flag or env var (flag wins):
#   --region REGION        (env AWS_REGION)       default us-east-1
#   --account ACCOUNT_ID   (env AWS_ACCOUNT_ID)   default: sts get-caller-identity
#   --tag TAG              (env IMAGE_TAG)         default latest (the platform's jobs never pin a digest)
#   --base-image URI       (env BASE_IMAGE)        overrides ALL stages' FROM (tier-0) at once
#   --torch-cpu-index URL  (env TORCH_CPU_INDEX)   CPU torch wheel index (default: Artifactory plain wheel)
#   --pip-index-url URL / --pip-trusted-host HOST  forwarded as build-args
#   --build-network NAME   (env BUILD_NETWORK)     set to 'sagemaker' inside Studio
#   --no-push              build only; skip ECR create/login/push
#
# Requires: docker daemon running; AWS creds with ECR + Secrets Manager permissions
# (unless --no-push); secretsmanager:GetSecretValue on the jfrog-token secret.
set -euo pipefail

# Every Dockerfile lives flat under src/images/job-<stage>[-<compute>]/, so the
# build context is the REPO ROOT, not this script's own directory.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

# ---- defaults (all overridable) --------------------------------------------
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="${AWS_ACCOUNT_ID:-}"
TAG="${IMAGE_TAG:-latest}"
# Default FROM: the platform's shared account base (novos-ar-base-cpu), in the
# same single ECR repo every image in this account shares. Resolved once
# ACCOUNT_ID is known (see the ECR account/registry block).
ECR_REPO_NAME="${ECR_REPO_NAME:-f1ai/applications/pwsa0002037}"
BASE_IMAGE_TAG="${BASE_IMAGE_TAG:-novos-ar-base-cpu}"
BASE_IMAGE="${BASE_IMAGE:-}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-}"
TORCH_CPU_INDEX="${TORCH_CPU_INDEX:-}"
BUILD_NETWORK="${BUILD_NETWORK:-}"
DO_PUSH=1

# Stage catalog: "<stage>|<job-dir>|<launcher>|<generic-repo>".
# Mirrors src/images/build_and_push.sh's STAGES_ALL, but the repo is the FIXED
# generic name rather than derived from --repo-prefix — there is no model
# identity here.
STAGES_ALL=(
  "preprocessing|job-preprocessing|preprocess_launcher.sh|novos-ar-base-cpu-job-preprocessing"
  "training-cpu|job-training-cpu|train_launcher.sh|novos-ar-base-cpu-job-training"
  "training-gpu|job-training-gpu|train_launcher.sh|novos-ar-base-gpu-job-training"
  "evaluation-cpu|job-evaluation-cpu|eval_launcher.sh|novos-ar-base-cpu-job-evaluation"
  "evaluation-gpu|job-evaluation-gpu|eval_launcher.sh|novos-ar-base-gpu-job-evaluation"
)

SELECTED=()

# ---- arg parse --------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)           REGION="$2"; shift 2;;
    --account)          ACCOUNT_ID="$2"; shift 2;;
    --tag)              TAG="$2"; shift 2;;
    --base-image)       BASE_IMAGE="$2"; shift 2;;
    --pip-index-url)    PIP_INDEX_URL="$2"; shift 2;;
    --pip-trusted-host) PIP_TRUSTED_HOST="$2"; shift 2;;
    --torch-cpu-index)  TORCH_CPU_INDEX="$2"; shift 2;;
    --build-network)    BUILD_NETWORK="$2"; shift 2;;
    --no-push)          DO_PUSH=0; shift;;
    -h|--help)          sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0;;
    -*)                 echo "[fail] unknown flag: $1" >&2; exit 2;;
    *)                  SELECTED+=("$1"); shift;;
  esac
done

# Which stages to build: all five (default), or just the positionals named
# (preprocessing / training-cpu / training-gpu / evaluation-cpu / evaluation-gpu).
build_list() {
  if [[ ${#SELECTED[@]} -eq 0 ]]; then
    printf '%s\n' "${STAGES_ALL[@]}"; return
  fi
  for want in "${SELECTED[@]}"; do
    local hit=""
    for row in "${STAGES_ALL[@]}"; do
      [[ "${row%%|*}" == "${want}" ]] && { echo "${row}"; hit=1; break; }
    done
    [[ -z "${hit}" ]] && {
      echo "[fail] unknown stage '${want}' (valid: preprocessing training-cpu training-gpu evaluation-cpu evaluation-gpu)" >&2
      exit 2
    }
  done
}
mapfile -t ROWS < <(build_list)

# ---- ECR account/registry (needed even with --no-push, to compute the default
# BASE_IMAGE below) + login (skipped with --no-push) --------------------------
[[ -z "${ACCOUNT_ID}" ]] && ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
if [[ "${DO_PUSH}" -eq 1 ]]; then
  echo "[login] ${ECR}"
  aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ECR}"
fi

[[ -z "${BASE_IMAGE}" ]] && BASE_IMAGE="${ECR}/${ECR_REPO_NAME}:${BASE_IMAGE_TAG}"

# ---- common build args ------------------------------------------------------
# HTTP(S)_PROXY/NO_PROXY: only passed when set in the environment — see
# src/images/build_and_push.sh's identical block for why.
COMMON_BUILD_ARGS=(--build-arg "BASE_IMAGE=${BASE_IMAGE}")
[[ -n "${PIP_INDEX_URL}"    ]] && COMMON_BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
[[ -n "${PIP_TRUSTED_HOST}" ]] && COMMON_BUILD_ARGS+=(--build-arg "PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}")
[[ -n "${TORCH_CPU_INDEX}"  ]] && COMMON_BUILD_ARGS+=(--build-arg "TORCH_CPU_INDEX=${TORCH_CPU_INDEX}")
BUILD_PROXY="${HTTPS_PROXY:-${HTTP_PROXY:-}}"
if [[ -n "${BUILD_PROXY}" ]]; then
  NO_PROXY_HOSTS="${NO_PROXY:-169.254.169.254,localhost,novartis.net,*.novartis.net}"
  COMMON_BUILD_ARGS+=(
    --build-arg "HTTP_PROXY=${BUILD_PROXY}" --build-arg "HTTPS_PROXY=${BUILD_PROXY}"
    --build-arg "NO_PROXY=${NO_PROXY_HOSTS}"
  )
  echo "[info] build-time proxy: ${BUILD_PROXY}"
else
  echo "[warn] no HTTP_PROXY/HTTPS_PROXY in the environment; pip may not reach pypi."
fi

# ---- fetch the ar_transformations sdist ONCE (shared by every stage) --------
# Every Dockerfile hard-fails its `pip install --no-build-isolation` step if no
# sdist is staged, generic build or not — these images still need the package
# importable. Same fetch as src/images/build_and_push.sh.
TRANSFORMATIONS_DIST="pwsa0002037-novaos-ar-transformations"
TRANSFORMATIONS_VERSION="${TRANSFORMATIONS_VERSION:-0.1.0}"
JFROG_SECRET_ID="${JFROG_SECRET_ID:-jfrog-token}"
JFROG_PYPI_REPO="${JFROG_PYPI_REPO:-f1-imu-im-pwsa0002058-pypi}"

TRANSFORMATIONS_SDIST_NAME="${TRANSFORMATIONS_DIST//-/_}-${TRANSFORMATIONS_VERSION}.tar.gz"
TRANSFORMATIONS_TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TRANSFORMATIONS_TMPDIR}"' EXIT
TRANSFORMATIONS_SDIST_PATH="${TRANSFORMATIONS_TMPDIR}/${TRANSFORMATIONS_SDIST_NAME}"
TRANSFORMATIONS_ARTIFACT_URL="https://artifactory.f1.novartis.net/artifactory/${JFROG_PYPI_REPO}/${TRANSFORMATIONS_DIST}/${TRANSFORMATIONS_VERSION}/${TRANSFORMATIONS_SDIST_NAME}"

echo "[info] fetching ${TRANSFORMATIONS_DIST} ${TRANSFORMATIONS_VERSION} from ${JFROG_PYPI_REPO}"
read -r JFROG_USER JFROG_TOKEN < <(
  aws secretsmanager get-secret-value --secret-id "${JFROG_SECRET_ID}" \
    --query SecretString --output text | jq -r 'to_entries[0] | "\(.key) \(.value)"'
) || {
  echo "[fail] could not read a user/token pair from secret ${JFROG_SECRET_ID}." >&2
  exit 1
}
if ! curl -sS -f -u "${JFROG_USER}:${JFROG_TOKEN}" \
     -o "${TRANSFORMATIONS_SDIST_PATH}" "${TRANSFORMATIONS_ARTIFACT_URL}"; then
  echo "[fail] could not download ${TRANSFORMATIONS_SDIST_NAME}." >&2
  echo "       Check that version is published and that ${JFROG_SECRET_ID} can read" >&2
  echo "       ${JFROG_PYPI_REPO}." >&2
  exit 1
fi
unset JFROG_TOKEN
echo "[info] staged ${TRANSFORMATIONS_SDIST_NAME} ($(wc -c <"${TRANSFORMATIONS_SDIST_PATH}") bytes)"

NET_ARGS=()
[[ -n "${BUILD_NETWORK}" ]] && NET_ARGS=(--network "${BUILD_NETWORK}")

# ---- build (+push) each selected stage -------------------------------------
for row in "${ROWS[@]}"; do
  IFS='|' read -r STAGE JOBDIR LAUNCHER REPO <<< "${row}"

  CTX="${REPO_ROOT}/src/images/${JOBDIR}"
  DOCKERFILE="${CTX}/Dockerfile"
  [[ -f "${DOCKERFILE}" ]] || { echo "[fail] ${STAGE}: missing ${DOCKERFILE}" >&2; exit 1; }

  # Stage the build context IN PLACE — code/ stays EMPTY (no model), matching
  # src/images/build_and_push.sh's checked-in-placeholder dirs. The launcher
  # (e.g. train_launcher.sh) is a permanent checked-in file in CTX already —
  # NOT staged. Overwrites whatever a prior per-model build left in code/,
  # which is exactly why the two scripts must not run concurrently (see header).
  rm -rf "${CTX}/code" "${CTX}/transformations"
  mkdir -p "${CTX}/code" "${CTX}/transformations"
  touch "${CTX}/code/.gitkeep" "${CTX}/transformations/.gitkeep"
  cp "${TRANSFORMATIONS_SDIST_PATH}" "${CTX}/transformations/${TRANSFORMATIONS_SDIST_NAME}"
  : > "${CTX}/requirements.txt"

  STAGE_BUILD_ARGS=(
    --build-arg "PLATFORM_ENTRY="
    --build-arg "PLATFORM_CONFIG="
    --build-arg "PLATFORM_EXTRA_ARGS="
  )

  echo ""
  echo "=================================================================="
  echo "[${STAGE}] repo=${REPO} tag=${TAG}  (GENERIC — no model code)"
  echo "  dockerfile=${JOBDIR}/Dockerfile"
  echo "=================================================================="
  echo "[stage] context ${CTX} contents:"
  ( cd "${CTX}" && find . -maxdepth 2 -type f | sort | sed 's/^/[stage]   /' )

  # ValidateVPCOnlyModeImage reads the FROM line BEFORE ARG substitution — see
  # src/images/build_and_push.sh's identical block for why this materialization
  # is needed.
  MATERIALISED="$(mktemp)"
  sed "s|^FROM .*|FROM ${BASE_IMAGE}|" "${DOCKERFILE}" > "${MATERIALISED}"

  echo "[build] docker build -> ${REPO}:${TAG}"
  docker build "${NET_ARGS[@]}" "${COMMON_BUILD_ARGS[@]}" "${STAGE_BUILD_ARGS[@]}" \
    -f "${MATERIALISED}" -t "${REPO}:${TAG}" "${CTX}"
  rm -f "${MATERIALISED}"

  if [[ "${DO_PUSH}" -eq 0 ]]; then
    echo "[done] built ${REPO}:${TAG} (--no-push: not pushing)"
    continue
  fi

  echo "[ecr ] ensure repo '${REPO}' exists ..."
  aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" \
         --image-scanning-configuration scanOnPush=true >/dev/null
  aws ecr put-image-tag-mutability --repository-name "${REPO}" \
    --image-tag-mutability MUTABLE --region "${REGION}" >/dev/null 2>&1 || true

  IMAGE_URI="${ECR}/${REPO}:${TAG}"
  echo "[push] tag + push ${IMAGE_URI} ..."
  docker tag "${REPO}:${TAG}" "${IMAGE_URI}"
  docker push "${IMAGE_URI}"
  echo "[done] ${IMAGE_URI}"
done

echo ""
echo "[all done] generic stages: $(printf '%s ' "${ROWS[@]%%|*}")"
