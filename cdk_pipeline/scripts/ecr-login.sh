#!/bin/bash
#####################################################################
# NOVOS AR — Authenticate Docker to this account's ECR registry.
#
# Its own script, invoked as `bash ./cdk_pipeline/scripts/ecr-login.sh`, because
# Jenkins' `sh` step runs /bin/sh
#####################################################################
set -euo pipefail
source ./cdk_pipeline/scripts/common.sh

aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
echo "✓ Docker authenticated to ${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
