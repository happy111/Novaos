#!/bin/bash
#####################################################################
# NOVOS AR — Build all src/images unconditionally (no change detection).
# Base images are built first so job images can FROM them.
# Usage: ./scripts/build-src-images.sh
#####################################################################
set -euo pipefail

export IMAGES_FILE=./cdk_pipeline/config-src-images.yaml
source ./cdk_pipeline/scripts/common.sh

P="http://proxy-euie.aws.novartis.net:3128"
NO_PROXY_HOSTS="${NO_PROXY:-169.254.169.254,localhost,novartis.net,*.novartis.net}"

# ---- fetch the ar_transformations sdist ONCE, shared by every job image ----
# job-feature-ingestion, job-cohort-label, and job-model-ready-dataset each
# COPY src/images/<job>/transformations/ and pip-install whatever .tar.gz
# lands there (see each Dockerfile) — that dir is checked in with only a
# .gitkeep, so without this the build either hard-fails (feature-ingestion)
# or silently ships without any transformation strategy (the other two).
# Mirrors src/images/build_and_push.sh's staging pattern. Credential is read
# from Secrets Manager by NAME, resolved in whichever account this script is
# authenticated to; it never becomes a build-arg or reaches an image layer.
TRANSFORMATIONS_DIST="pwsa0002037-novaos-ar-transformations"
TRANSFORMATIONS_VERSION="${TRANSFORMATIONS_VERSION:-0.1.0}"
JFROG_SECRET_ID="${JFROG_SECRET_ID:-jfrog-token}"
JFROG_PYPI_REPO="${JFROG_PYPI_REPO:-f1-imu-im-pwsa0002058-pypi}"
TRANSFORMATIONS_SDIST_NAME="${TRANSFORMATIONS_DIST//-/_}-${TRANSFORMATIONS_VERSION}.tar.gz"
TRANSFORMATIONS_TMPDIR="$(mktemp -d)"
TRANSFORMATIONS_SDIST_PATH="${TRANSFORMATIONS_TMPDIR}/${TRANSFORMATIONS_SDIST_NAME}"
TRANSFORMATIONS_ARTIFACT_URL="https://artifactory.f1.novartis.net/artifactory/${JFROG_PYPI_REPO}/${TRANSFORMATIONS_DIST}/${TRANSFORMATIONS_VERSION}/${TRANSFORMATIONS_SDIST_NAME}"

# Clean up the tmpdir AND every job dir we staged the sdist into, even on failure —
# these dirs are part of the real checkout, not a throwaway build context.
trap 'rm -rf "${TRANSFORMATIONS_TMPDIR}"; find src/images -maxdepth 3 -path "*/transformations/${TRANSFORMATIONS_SDIST_NAME}" -delete 2>/dev/null' EXIT

echo "[info] fetching ${TRANSFORMATIONS_DIST} ${TRANSFORMATIONS_VERSION} from ${JFROG_PYPI_REPO}"
read -r JFROG_USER JFROG_TOKEN < <(
    aws secretsmanager get-secret-value --secret-id "${JFROG_SECRET_ID}" --region "${REGION}" \
        --query SecretString --output text | jq -r 'to_entries[0] | "\(.key) \(.value)"'
) || {
    echo "[fail] could not read a user/token pair from secret ${JFROG_SECRET_ID}." >&2
    exit 1
}
if ! curl -sS -f -u "${JFROG_USER}:${JFROG_TOKEN}" \
     -o "${TRANSFORMATIONS_SDIST_PATH}" "${TRANSFORMATIONS_ARTIFACT_URL}"; then
    echo "[fail] could not download ${TRANSFORMATIONS_SDIST_NAME}." >&2
    echo "       Check that version is published and that ${JFROG_SECRET_ID} can read ${JFROG_PYPI_REPO}." >&2
    exit 1
fi
unset JFROG_TOKEN
echo "[info] staged ${TRANSFORMATIONS_SDIST_NAME} ($(wc -c <"${TRANSFORMATIONS_SDIST_PATH}") bytes)"

_build() {
    local name=$1
    local img
    img=$(yq ".images[] | select(.name == \"$name\")" "$IMAGES_FILE")

    local tag_var dockerfile dir platform
    tag_var=$(   yq '.tag_var'        <<<"$img")
    dockerfile=$(yq '.dockerfile'     <<<"$img")
    dir=$(       yq '.dir'            <<<"$img")
    platform=$(  yq '.platform // ""' <<<"$img")
    local tag=${!tag_var}

    local proxy_args=()
    if [[ -n "${CI:-}" ]]; then
        proxy_args=(
            --build-arg HTTP_PROXY="$P"  --build-arg HTTPS_PROXY="$P"
            --build-arg http_proxy="$P"  --build-arg https_proxy="$P"
            --build-arg NO_PROXY="$NO_PROXY_HOSTS"
            --build-arg no_proxy="$NO_PROXY_HOSTS"
        )
    fi

    local build_args=()
    for n in $(yq '.build_args[]?' <<<"$img"); do
        build_args+=(--build-arg "$n=${!n}")
    done
    [[ -n "$platform" ]] && build_args+=(--platform "$platform")

    if [[ -d "${dir}/transformations" ]]; then
        cp "${TRANSFORMATIONS_SDIST_PATH}" "${dir}/transformations/${TRANSFORMATIONS_SDIST_NAME}"
        echo "[info] staged ${TRANSFORMATIONS_SDIST_NAME} into ${dir}/transformations/"
    fi

    echo "────────────────────────────────────────────"
    echo "  Building: ${name} → ${ECR_REPO}:${tag}"
    echo "────────────────────────────────────────────"
    docker build \
        -t "${ECR_REPO}:${tag}" \
        -f "$dockerfile" \
        "${proxy_args[@]}" \
        "${build_args[@]}" \
        .

    docker image inspect "${ECR_REPO}:${tag}" >/dev/null 2>&1 \
        || { echo "❌ ${name} image not found locally after build" >&2; exit 1; }
    echo "✓ ${name} built: ${ECR_REPO}:${tag}"
    return 0
}

# ── Step 1: Base images first (job images FROM these) ─────────────
for name in $(yq -r '.images[] | select(.fixed_tag) | .name' "$IMAGES_FILE"); do
    _build "$name"
done

# ── Step 2: Job images ─────────────────────────────────────────────
for name in $(yq -r '.images[] | select(.fixed_tag | not) | .name' "$IMAGES_FILE"); do
    _build "$name"
done

echo "═══════════════════════════════════════════════"
echo "  All src images built successfully"
echo "═══════════════════════════════════════════════"
