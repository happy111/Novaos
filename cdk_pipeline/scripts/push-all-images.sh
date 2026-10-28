#!/bin/bash
#####################################################################
# NOVOS AR — Push all Docker images to ECR (loops over config.yaml)
# No arguments needed — discovers images from config.yaml
#####################################################################
set -euo pipefail
source ./cdk_pipeline/scripts/common.sh

IMAGES_FILE="${IMAGES_FILE:-./cdk_pipeline/config.yaml}"
SCRIPT_DIR="$(dirname "$0")"

for image_name in $(yq -r '.images[].name' "${IMAGES_FILE}"); do
    echo "────────────────────────────────────────────"
    echo "  Pushing: ${image_name}"
    echo "────────────────────────────────────────────"
    bash "${SCRIPT_DIR}/push-image.sh" "${image_name}"
done

echo "═══════════════════════════════════════════════"
echo "  All images pushed successfully"
echo "═══════════════════════════════════════════════"
