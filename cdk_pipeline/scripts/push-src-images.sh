#!/bin/bash
#####################################################################
# NOVOS AR — Push all src/images to ECR unconditionally.
# Usage: ./scripts/push-src-images.sh
#####################################################################
set -euo pipefail

export IMAGES_FILE=./cdk_pipeline/config-src-images.yaml
source ./cdk_pipeline/scripts/common.sh

: "${ECR_REPO:?ERROR: ECR_REPO is not set}"
: "${ACCOUNT:?ERROR: ACCOUNT is not set}"
: "${REGION:?ERROR: REGION is not set}"

aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

for name in $(yq -r '.images[].name' "$IMAGES_FILE"); do
    img=$(yq ".images[] | select(.name == \"$name\")" "$IMAGES_FILE")
    tag_var=$(yq '.tag_var' <<<"$img")
    tag="${!tag_var}"

    docker image inspect "${ECR_REPO}:${tag}" >/dev/null 2>&1 \
        || { echo "⚠ ${name} image not found locally — was it built? Skipping push." >&2; continue; }

    echo "────────────────────────────────────────────"
    echo "  Pushing: ${name} → ${ECR_REPO}:${tag}"
    echo "────────────────────────────────────────────"
    docker push "${ECR_REPO}:${tag}"
    echo "✓ ${name} pushed: ${ECR_REPO}:${tag}"
done

echo "═══════════════════════════════════════════════"
echo "  All src images pushed successfully"
echo "═══════════════════════════════════════════════"
