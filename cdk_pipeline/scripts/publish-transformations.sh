#!/bin/bash
#####################################################################
# NOVOS AR — Build and publish transformations package to JFrog PyPI
#####################################################################
set -euo pipefail

TRANSFORMATIONS_REPO_URL="${TRANSFORMATIONS_REPO_URL:-https://bitbucketenterprise.aws.novartis.net/scm/pwsa0002037/pwsa0002037_novaos_ar_transformations.git}"
TRANSFORMATIONS_REF="${TRANSFORMATIONS_REF:-master}"
JFROG_PYPI_REPO="${JFROG_PYPI_REPO:-f1-imu-im-pwsa0002058-pypi}"
JFROG_USER="${JFROG_USER:-sahaar9}"

: "${JFROG_TOKEN:?ERROR: JFROG_TOKEN is not set}"

WORK_DIR="${TRANSFORMATIONS_SRC_DIR:-transformations-src}"
VENV_DIR=".venv-transformations-publish"

rm -rf "${VENV_DIR}"

if [[ ! -d "${WORK_DIR}" ]]; then
  echo "ERROR: Transformations source folder '${WORK_DIR}' not found." >&2
  echo "Expected Jenkins to checkout ${TRANSFORMATIONS_REPO_URL} (${TRANSFORMATIONS_REF}) into ${WORK_DIR}." >&2
  exit 1
fi

python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip build twine

cd "${WORK_DIR}"
python -m build
python -m twine upload \
  --repository-url "https://artifactory.f1.novartis.net/artifactory/api/pypi/${JFROG_PYPI_REPO}" \
  -u "${JFROG_USER}" \
  -p "${JFROG_TOKEN}" \
  dist/*

echo "✓ Transformations package published successfully"
