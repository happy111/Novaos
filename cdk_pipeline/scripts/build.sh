#!/bin/bash
#####################################################################
# NOVOS AR — CDK Synth (build CloudFormation templates)
#####################################################################

set -ex

source ./cdk_pipeline/scripts/common.sh
source .venv/bin/activate

cd cdk_pipeline
cdk synth --context "environment=${ENVIRONMENT}"
