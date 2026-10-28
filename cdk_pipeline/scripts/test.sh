#!/bin/bash
#####################################################################
# NOVOS AR — Run CDK/Python tests
#####################################################################

set -e

source ./cdk_pipeline/scripts/common.sh
source ./.venv/bin/activate
if [[ -z "${CI}" ]]; then
    echo "This is local execution"
    pytest -svvv ./cdk_pipeline/tests --cov=./cdk_pipeline --cov-report=term-missing
else
    echo "This is CI/CD execution"
    pytest -svvv --cov=. --cov-report=xml:test_unit_coverage.xml --junitxml=test_unit_results.xml
fi
