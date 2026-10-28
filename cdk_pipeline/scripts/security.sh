#!/bin/bash
#####################################################################
# NOVOS AR — Security scanning (bandit + git-secrets)
#####################################################################

set -e

source .venv/bin/activate

if [[ -z "${CI}" ]]; then
    echo "This is local execution"
    git secrets --scan -r .
fi

bandit -c pyproject.toml -r . --severity-level high
