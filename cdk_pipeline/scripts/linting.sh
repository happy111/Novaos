#!/bin/bash
#####################################################################
# NOVOS AR — Code linting
#####################################################################

set -e

source .venv/bin/activate
if [[ -z "${CI}" ]]; then
    echo "This is local execution"
    black .
    isort . --profile black --filter-files
    ruff check .
else
    echo "This is CI/CD execution"
    black . --check
    isort . --profile black --filter-files --check-only
    ruff check .
fi
