#!/bin/bash
#####################################################################
# NOVOS SL — Push a Docker image to ECR (skips if already exists)
# Usage: ./scripts/push-image.sh <image-name>
#####################################################################
set -euo pipefail
source ./cdk_pipeline/scripts/common.sh

IMAGES_FILE="${IMAGES_FILE:-./cdk_pipeline/config.yaml}"

case "${1:-}" in
  "") echo "Usage: $0 <$(yq '.images[].name' "$IMAGES_FILE" | paste -sd '|')>" >&2; exit 1 ;;
  *) ;;
esac

img=$(yq ".images[] | select(.name == \"$1\")" "$IMAGES_FILE")
[[ -n "$img" && "$img" != "null" ]] || { echo "Unknown image: $1" >&2; exit 1; }

tag_var=$(  yq '.tag_var'             <<<"$img")
alias_var=$(yq '.alias_tag_var // ""' <<<"$img")
TAG="${!tag_var}"
EXTRA=""
[[ -n "$alias_var" ]] && EXTRA="${!alias_var}"

echo "Checking if image ${TAG} already exists in ECR"
if aws ecr describe-images \
    --repository-name "${ECR_REPO_NAME}" \
    --image-ids imageTag="${TAG}" \
    --region "${REGION}" \
    >/dev/null 2>&1; then
    echo "Image ${TAG} already exists in ECR. Skipping push."
    exit 0
fi

echo "Image not found in ECR. Pushing..."
aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

docker image inspect "${ECR_REPO}:${TAG}" >/dev/null \
    || { echo "Local image ${ECR_REPO}:${TAG} not found — did the build stage run?" >&2; exit 1; }

echo "Pushing Docker image ${ECR_REPO}:${TAG}"
docker push "${ECR_REPO}:${TAG}"
[[ -n "${EXTRA}" ]] && { echo "Pushing alias tag ${EXTRA}..."; docker push "${ECR_REPO}:${EXTRA}"; }

echo "----- ✓ Successfully pushed $1 image: ${TAG} ------"
