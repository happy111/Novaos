#!/bin/bash
#####################################################################
# NOVOS AR — Shared environment configuration
# Determines environment based on BRANCH_NAME and exports all
# variables used by sibling scripts.
#####################################################################

# Determine environment based on branch name
if [[ "${BRANCH_NAME}" == "validate" ]]; then
    ENV_FILE="./cdk_pipeline/environments/qa.json"
    export ENVIRONMENT="qa"

elif [[ "${BRANCH_NAME}" == "master" ]]; then
    ENV_FILE="./cdk_pipeline/environments/prod.json"
    export ENVIRONMENT="prod"

else
    ENV_FILE="./cdk_pipeline/environments/dev.json"
    export ENVIRONMENT="dev"
fi

# ── Validate env file exists ─────────────────────────────────────
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: Environment config not found: ${ENV_FILE}" >&2
    exit 1
fi

# ── Read and validate required fields from env JSON ──────────────
ACCOUNT="$(jq -r .account "${ENV_FILE}")" \
    || { echo "ERROR: Failed to parse ${ENV_FILE}" >&2; exit 1; }
: "${ACCOUNT:?ERROR: .account missing in ${ENV_FILE}}"
export ACCOUNT

APP_PREFIX="$(jq -r .app_prefix "${ENV_FILE}")" \
    || { echo "ERROR: Failed to read app_prefix from ${ENV_FILE}" >&2; exit 1; }
: "${APP_PREFIX:?ERROR: .app_prefix missing in ${ENV_FILE}}"
export APP_PREFIX

STACK_PREFIX="$(jq -r .stack_prefix "${ENV_FILE}")" \
    || { echo "ERROR: Failed to read stack_prefix from ${ENV_FILE}" >&2; exit 1; }
: "${STACK_PREFIX:?ERROR: .stack_prefix missing in ${ENV_FILE}}"
export STACK_PREFIX

# ── AWS Configuration ────────────────────────────────────────────
export REGION="us-east-1"

# ── ECR Configuration ────────────────────────────────────────────
export ECR_REPO_NAME="$(jq -r '.ecr_repo_name // ""' "${ENV_FILE}")"
: "${ECR_REPO_NAME:?ERROR: ecr_repo_name missing in ${ENV_FILE}}"
export ECR_REPO="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"

# ── Alias so scripts can reference ENV as a build-arg ────────────
export ENV="${ENVIRONMENT}"

# ── Derive per-image tag env vars ────────────────────────────────
IMAGES_FILE="${IMAGES_FILE:-./cdk_pipeline/config.yaml}"
if [[ ! -f "${IMAGES_FILE}" ]]; then
    echo "ERROR: Image config not found: ${IMAGES_FILE}" >&2
    exit 1
fi

for image_name in $(yq -r '.images[].name' "${IMAGES_FILE}"); do
    tag_var=$(yq   -r ".images[] | select(.name == \"$image_name\") | .tag_var"                "${IMAGES_FILE}")
    fixed_tag=$(yq -r ".images[] | select(.name == \"$image_name\") | .fixed_tag // \"\""      "${IMAGES_FILE}")
    prefix=$(yq    -r ".images[] | select(.name == \"$image_name\") | .tag_prefix // \"\""     "${IMAGES_FILE}")
    dir=$(yq       -r ".images[] | select(.name == \"$image_name\") | .dir"                     "${IMAGES_FILE}")
    alias_var=$(yq -r ".images[] | select(.name == \"$image_name\") | .alias_tag_var // \"\""   "${IMAGES_FILE}")
    alias_val=$(yq -r ".images[] | select(.name == \"$image_name\") | .alias_tag_value // \"\"" "${IMAGES_FILE}")

    : "${tag_var:?ERROR: tag_var missing for image ${image_name} in ${IMAGES_FILE}}"

    if [[ -n "${fixed_tag}" ]]; then
        export "${tag_var}=${fixed_tag}"
    else
        : "${prefix:?ERROR: tag_prefix missing for image ${image_name} in ${IMAGES_FILE}}"
        hash=$(git log -1 --format=%h -- "./${dir}" 2>/dev/null)
        if [[ -z "${hash}" ]]; then
            echo "WARNING: No git history found for ${dir} — using HEAD commit SHA"
            hash=$(git rev-parse --short HEAD)
        fi
        export "${tag_var}=${prefix}-${hash}-${ENVIRONMENT}"
        if [[ -n "$alias_var" ]]; then
            export "${alias_var}=${alias_val}-${ENVIRONMENT}"
        fi
    fi
done

# ── Derived base image URIs (used as --build-arg in job image builds) ────────
export BASE_CPU_IMAGE="${ECR_REPO}:${BASE_CPU_IMAGE_TAG:-}"

# ── Sonar Configuration ───────────────────────────────────────────
export SONAR_SCANNER_VERSION="6.2.0.4584"  # NOSONAR
export SONAR_PROJECT_KEY="${SONAR_PROJECT_KEY:-f1-imu-im-pwsa0002037}"
export SONAR_SCANNER_HOME="${HOME}/.sonar/sonar-scanner-${SONAR_SCANNER_VERSION}-linux-x64"
if [[ -z "${SONAR_HOST_URL:-}" ]]; then
    export SONAR_HOST_URL="https://sonarqube.f1.novartis.net"
fi

# Duplication-detection (CPD) exclusions. These files are still analysed for issues,
# coverage and security — only the copy-paste metric skips them. Duplication is a
# MEASURE, not an issue, so it cannot be marked "won't fix" in the UI; an exclusion is
# the only way to accept a known, deliberate repeat.
#
# The three serving/app.py copies are the platform's generic inference entrypoint,
# byte-identical by design: job-inference-cpu and job-inference-gpu each need their
# own copy baked into their own image (no shared base either builds FROM), and
# src/images/default_containers/serving/app.py is a leftover duplicate from an
# earlier directory layout — kept for now, not yet removed. Extracting a shared
# module would change what each Dockerfile COPYs into its image, which is a bigger
# change than a Sonar exclusion; this stays until that cleanup happens.
export SONAR_CPD_EXCLUSIONS="cdk_pipeline/cdk_stacks/cdk_iam_deploy_roles.py,src/images/job-inference-cpu/serving/app.py,src/images/job-inference-gpu/serving/app.py,src/images/default_containers/serving/app.py"

# Per-file rule suppressions, as "ruleKey|path" pairs. sonar.sh turns each into a
# -Dsonar.issue.ignore.multicriteria block. Every entry needs a reason below: a
# suppression is a decision kept in git (where it is reviewable in a PR) instead of
# in per-branch UI clicks, which matters because hotspot/issue review state in
# SonarQube is PER BRANCH and does not propagate to develop or to anyone else's
# branch — without this, every branch re-reviews the same findings forever.
#
# ── docker:S6471 ("default user as root") × 5 job images — FALSE POSITIVE ────
# These five Dockerfiles deliberately END on `USER root`. SageMaker mounts the
# output dir root-owned, so each image's ENTRYPOINT launcher must start as root to
# chown it, then drops to appuser via
# `setpriv --reuid appuser --regid appuser --clear-groups` before running any job
# code — the arbitrary-model-code surface never runs as root. The other 17
# Dockerfiles carrying `USER root` are NOT excluded: they end on `USER appuser`
# (or `mlops`), so Sonar does not flag them and nothing here hides them.
#
# NOT solved with an inline `# NOSONAR`: Docker's legacy-builder USER parser takes
# a trailing comment as part of the username, which broke the image build (fixed in
# 5f343be/33d4e0d). Moving the comment to its own line keeps the build working but
# drops the suppression, which is what surfaced these five.
#
# ── docker:S6596 (":latest" base image) × neptune-check — DEFERRED, NOT FIXED ──
# This one is a REAL finding being deliberately deferred, not refuted. An unpinned
# `:latest` base image genuinely is a reproducibility problem, and this suppression
# hides it rather than solving it.
#
# The actual fix is NOT a version pin. neptune-check is a 21-line dummy health-check
# handler (imports json/os; requirements.txt is just `pandas`) that is currently
# built FROM `f1-tools-docker-images/jenkins-slave/inbound-agent-eu-2555:latest` —
# a Jenkins BUILD AGENT image, deployed as a Lambda runtime. That looks like a
# copy-paste from the Jenkins agent config rather than a deliberate choice, and it
# is why the tag cannot be pinned from here: the Artifactory Docker registry API
# returns NAME_UNKNOWN for that path under this project's jfrog-token, because the
# project has no business reading that repo.
#
# The right change is to rebase it on the repo's own already-pinned base,
# `ecr-docker-proxy/docker/library/python:3.13-slim` (see
# src/images/base-cpu/Dockerfile:1) — which satisfies S6596 by construction, needs
# no credential, matches how the other three lambdas get their base via
# ARG BASE_IMAGE, and drops a few hundred MB of irrelevant CI tooling from a Lambda
# image. That is a behavioural change to a deployed image: it needs a real build
# plus a Lambda invoke to verify, not just a green scan, so it is not being done
# inside a Sonar-cleanup branch. Remove this entry when that lands.
export SONAR_ISSUE_IGNORES=(
    "docker:S6471|src/images/job-evaluation-cpu/Dockerfile"
    "docker:S6471|src/images/job-evaluation-gpu/Dockerfile"
    "docker:S6471|src/images/job-preprocessing/Dockerfile"
    "docker:S6471|src/images/job-training-cpu/Dockerfile"
    "docker:S6471|src/images/job-training-gpu/Dockerfile"
    "docker:S6596|src/lambda/neptune-check/Dockerfile"
)

# ── Print configuration ───────────────────────────────────────────
echo "=== NOVOS AR Configuration ==="
echo "Environment: ${ENVIRONMENT}"
echo "Account: ${ACCOUNT}"
echo "Region: ${REGION}"
echo "ECR Repository: ${ECR_REPO_NAME}"
echo "==============================="
