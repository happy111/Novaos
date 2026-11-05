#!/usr/bin/env bash
# Create the virtual environment every runbook script runs in, and report the
# command-line tools they shell out to.
#
# Run once per checkout, from anywhere:
#
#   pwsa0002037_novaos_ar/docs/runbooks/setup_venv.sh
#
# Then activate it in each shell that runs a runbook step:
#
#   source pwsa0002037_novaos_ar/.venv-runbooks/bin/activate
#
#   VENV_DIR   where to create it (default: <repo>/.venv-runbooks)
#   ENV        environment file to name in the summary, dev | qa | prod (default: dev)
#
# Shared by the feature-ingestion, cohort-label, model-ready-dataset and
# model-training runbooks: they import the same handful of packages and shell out to
# the same `aws`, so one environment serves them all and there is one place to fix
# when that set changes.
#
# Idempotent — an existing venv is reused and upgraded in place, so re-running is
# how you pick up a requirements.txt change.
#
# Installs only what the scripts import (requirements.txt beside this file), never
# the repo's test or CDK dependencies. `docker` and `jq` are needed by
# build_and_push.sh alone and cannot be pip installed, so they are checked and
# reported rather than installed.
set -euo pipefail

# pyproject.toml's requires-python. The scripts import src/common, which is written
# against it, so an older interpreter fails on syntax rather than on a missing
# package — worth catching here instead of mid-run.
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-runbooks}"
ENV_NAME="${ENV:-dev}"
REQUIREMENTS="${REPO_ROOT}/docs/runbooks/requirements.txt"

[[ -f "$REQUIREMENTS" ]] ||
  { echo "[fail] no requirements file at $REQUIREMENTS" >&2; exit 1; }

# The newest python3 on PATH that satisfies the floor. Named candidates first so a
# distro whose `python3` is too old still works when a newer one is installed
# alongside it.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)"; then
    PYTHON="$candidate"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "[fail] no python3 >= ${PY_MIN_MINOR} on PATH (found: $(command -v python3 || echo none))." >&2
  echo "       The runbook scripts import src/common, which requires Python >= 3.${PY_MIN_MINOR}." >&2
  exit 1
fi
echo "[info] interpreter  : $("$PYTHON" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

if [[ -d "$VENV_DIR" ]]; then
  echo "[info] venv         : $VENV_DIR (exists — upgrading in place)"
else
  echo "[info] venv         : $VENV_DIR (creating)"
  "$PYTHON" -m venv "$VENV_DIR"
fi

# Called rather than activated: activation only edits PATH for this shell, and the
# absolute path is unambiguous about which environment is being filled.
"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip
echo "[info] installing   : $REQUIREMENTS"
"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade -r "$REQUIREMENTS"

# The scripts put the repo root on sys.path and import src/common from it, so the
# import is what proves the environment, not the package list.
"${VENV_DIR}/bin/python" - "$REPO_ROOT" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import boto3  # noqa: F401
import pandas  # noqa: F401
import yaml  # noqa: F401

from src.common import intake_schema, cohort_label_schema, mrd_schema  # noqa: F401

print("[ok]   imports     : boto3, pandas, yaml, src.common.* all resolve")
PY

echo "[ok]   aws          : $("${VENV_DIR}/bin/aws" --version 2>&1)"

# build_and_push.sh only. Reported as warnings so someone who is just running a
# pipeline against an already-pushed image is not blocked by a missing Docker.
MISSING=()
command -v docker >/dev/null 2>&1 || MISSING+=("docker")
command -v jq >/dev/null 2>&1 || MISSING+=("jq")
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "[warn] not on PATH : ${MISSING[*]}"
  echo "       Needed by build_and_push.sh only — pip cannot install either. Every"
  echo "       other step works without them."
fi

# Non-fatal: the venv is correct whether or not credentials happen to be loaded now.
if ! "${VENV_DIR}/bin/aws" sts get-caller-identity >/dev/null 2>&1; then
  echo "[warn] no usable AWS credentials in this shell. Outside SageMaker there is no"
  echo "       ambient role, so configure them before the upload and run steps:"
  echo "         aws configure   (or export AWS_PROFILE / AWS_ACCESS_KEY_ID ...)"
else
  echo "[ok]   aws identity : $("${VENV_DIR}/bin/aws" sts get-caller-identity --query Arn --output text)"
fi

cat <<SUMMARY

[ok] ready. Activate it in every shell that runs a runbook step:

       source ${VENV_DIR}/bin/activate

     Then follow the runbook from its next step. Environment files are read from
     ${REPO_ROOT}/cdk_pipeline/environments/ (ENV=${ENV_NAME}).
SUMMARY
