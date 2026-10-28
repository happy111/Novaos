#!/bin/bash
#####################################################################
# NOVOS AR — Wiz container image scanning
#####################################################################

set -e

source ./cdk_pipeline/scripts/common.sh
chmod +x ./wizcli
mkdir -p ./wiz-reports

IMAGES_FILE=${IMAGES_FILE:-./cdk_pipeline/config.yaml}

: "${ECR_REPO:?ERROR: ECR_REPO is not set}"
: "${WIZ_CLIENT_ID:?ERROR: WIZ_CLIENT_ID is not set}"
: "${WIZ_CLIENT_SECRET:?ERROR: WIZ_CLIENT_SECRET is not set}"

# Collect all tags main plus alias from config.yaml
tags=()
for tv in $(yq -r '.images[] | (.tag_var, (.alias_tag_var // ""))' "$IMAGES_FILE" | grep -v '^$'); do
    [[ -n "${!tv:-}" ]] && tags+=("${!tv}")
done

for image_tag in "${tags[@]}"; do
    if ! docker image inspect "${ECR_REPO}:${image_tag}" >/dev/null 2>&1; then
        echo "Skipping Wiz scan for ${ECR_REPO}:${image_tag} because image is not built locally."
        continue
    fi

    echo "Running Wiz scan for ${ECR_REPO}:${image_tag}"

    set +e
    ./wizcli scan container-image "${ECR_REPO}:${image_tag}" \
        --by-policy-hits DISABLED \
        --client-id "${WIZ_CLIENT_ID}" \
        --client-secret "${WIZ_CLIENT_SECRET}" \
        --human-output-file "./wiz-reports/wizScanResult-${image_tag}.log" \
        --json-output-file "./wiz-reports/wizScanResult-${image_tag}.json" \
        --timeout 1h0m0s
    wiz_exit_code=$?
    set -e

    echo "============= WIZ SCAN SUMMARY (${image_tag}) ============="

    if [[ -f "./wiz-reports/wizScanResult-${image_tag}.log" ]]; then
        sed -n '/^Scan summary:/,$p' "./wiz-reports/wizScanResult-${image_tag}.log" || true
    else
        echo "No Wiz human-readable report found for ${image_tag}"
    fi

    echo "============================================================"

    if [[ ${wiz_exit_code} -ne 0 ]]; then
        echo "ERROR: Wiz scan failed due to an operational issue. Exit code: ${wiz_exit_code}" >&2
        exit "${wiz_exit_code}"
    fi
done
