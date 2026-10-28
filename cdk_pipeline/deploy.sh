#!/bin/bash
set -e
ENV="${1:-dev}"
echo "Deploying AR Platform — env: ${ENV}"
if [[ ! -f "environments/${ENV}.json" ]]; then
    echo "ERROR: environments/${ENV}.json not found" >&2; exit 1
fi
cdk synth --all --context "env=${ENV}"
cdk deploy --all --context "env=${ENV}" --require-approval never
echo "Done — 7 stacks deployed for: ${ENV}"
