#!/bin/bash
#####################################################################
# NOVOS AR — CDK Diff (show pending infra changes)
#####################################################################

set -e

source ./cdk_pipeline/scripts/common.sh
source .venv/bin/activate

cd cdk_pipeline

# Optional: comma-separated stack names to diff (e.g. "S3Buckets,Lambdas").
# Defaults to all stacks. Used by feature/* branches to target specific
# stacks via DEPLOY_STACKS.
STACK_NAMES="${1:-}"
DIFF_TARGET_ARGS=()
if [[ -n "${STACK_NAMES}" ]]; then
    IFS=',' read -ra NAMES <<< "${STACK_NAMES}"
    for name in "${NAMES[@]}"; do
        DIFF_TARGET_ARGS+=("${STACK_PREFIX}-${name}-${APP_PREFIX}-${ENVIRONMENT}")
    done
fi

if [[ -z "${CI}" ]]; then
    echo "This is local execution"
    cdk diff "${DIFF_TARGET_ARGS[@]}" --app cdk.out --context "environment=${ENVIRONMENT}"
else
    echo "This is CI/CD execution"
    export AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    export NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

    CDK_OUTPUT=$(cdk diff "${DIFF_TARGET_ARGS[@]}" --app cdk.out --ci --context "environment=${ENVIRONMENT}" 2>&1)
    CDK_EXIT_CODE=$?
    echo "$CDK_OUTPUT"

    if echo "$CDK_OUTPUT" | grep -qE "self.signed certificate|unable to get local issuer|certificate has expired|certificate verify failed"; then
        echo "ERROR: CDK diff failed due to a TLS/certificate issue." >&2
        exit 1
    fi
    exit $CDK_EXIT_CODE
fi
