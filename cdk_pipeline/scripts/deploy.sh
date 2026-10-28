#!/bin/bash
#####################################################################
# NOVOS AR — CDK Deploy (deploy all stacks)
#####################################################################

set -e

source ./cdk_pipeline/scripts/common.sh
source .venv/bin/activate

cd cdk_pipeline

# Optional: comma-separated stack names to deploy (e.g. "S3Buckets,Lambdas").
# Defaults to all stacks. Used by feature/* branches to target specific
# stacks via DEPLOY_STACKS.
STACK_NAMES="${1:-}"
DEPLOY_TARGETS=()
if [[ -n "${STACK_NAMES}" ]]; then
    IFS=',' read -ra NAMES <<< "${STACK_NAMES}"
    for name in "${NAMES[@]}"; do
        DEPLOY_TARGETS+=("${STACK_PREFIX}-${name}-${APP_PREFIX}-${ENVIRONMENT}")
    done
else
    DEPLOY_TARGETS=("--all")
fi

if [[ -z "${CI}" ]]; then
    echo "This is local execution"
    cdk deploy "${DEPLOY_TARGETS[@]}" --app cdk.out --context "environment=${ENVIRONMENT}"
else
    echo "This is CI/CD execution"
    export AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    export NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
    cdk deploy "${DEPLOY_TARGETS[@]}" --app cdk.out --ci --context "environment=${ENVIRONMENT}" --require-approval never
fi
