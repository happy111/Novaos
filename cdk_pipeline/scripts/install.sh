#!/bin/bash
#####################################################################
# NOVOS AR — Install dependencies (CI and local)
#####################################################################

set -e

source ./cdk_pipeline/scripts/common.sh

# ── Pinned tool versions ─────────────────────────────────────────
# Override in the environment to bump without editing this script.
AWS_CDK_VERSION="${AWS_CDK_VERSION:-2.1142.0}"
YQ_VERSION="${YQ_VERSION:-v4.53.6}"
YQ_SHA256="${YQ_SHA256:-c5f056448f973ae7d39b5401949648a78f2dc1947d6a8eb65be60d5c504b9385}"

CURL_HTTPS=(--proto '=https' --tlsv1.2 -fsSL)

configure_pip() {
    local artifactory_url="https://artifactory.f1.novartis.net/artifactory/api/pypi/pypi-proxy-nextgen/simple"
    echo "Configuring pip with Artifactory URL: $artifactory_url"
    pip config set global.index-url $artifactory_url
    return 0
}

if [[ -z "${CI}" ]]; then
    echo "This is local execution"
    python -m venv .venv
else
    echo "This is CI/CD execution"
    # CA certificate. Fetched over HTTPS: this file becomes a system trust
    # anchor, so over cleartext a MITM could install its own root CA and then
    # transparently intercept every HTTPS download below it. -f so an error page
    # is never written to the .crt path, and the PEM header is checked before the
    # cert is trusted — update-ca-certificates skips malformed files silently.
    curl "${CURL_HTTPS[@]}" \
        -o /tmp/SILVERROOT.crt https://pki.novartis.com/aia/SILVERROOT.crt
    grep -q "BEGIN CERTIFICATE" /tmp/SILVERROOT.crt \
        || { echo "ERROR: SILVERROOT.crt is not a PEM certificate" >&2; exit 1; }
    install -m 644 /tmp/SILVERROOT.crt /usr/local/share/ca-certificates/SILVERROOT.crt
    rm -f /tmp/SILVERROOT.crt
    update-ca-certificates

    # Node.js + CDK. --proto '=https' refuses any non-HTTPS hop including a
    # redirect that tries to downgrade, which matters most here: the response is
    # piped straight into a shell.
    curl "${CURL_HTTPS[@]}" https://deb.nodesource.com/setup_20.x | bash -
    apt-get update -y
    apt-get install -y nodejs
    # --ignore-scripts: a global install runs postinstall hooks as root and the
    # CDK CLI needs none. Pinned so an infrastructure deploy is reproducible.
    npm install -g --ignore-scripts "aws-cdk@${AWS_CDK_VERSION}"
    # Python venv
    python3 -m venv .venv

    # yq — needed by build-image.sh / push-image.sh to read config.yaml.
    # Pinned and checksum-verified: this binary lands on PATH as root, and
    # /releases/latest/ is a moving target with no integrity check at all.
    if ! command -v yq >/dev/null 2>&1; then
        echo "Installing yq ${YQ_VERSION} (Go) to /usr/local/bin"
        curl "${CURL_HTTPS[@]}" \
            "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_amd64" \
            -o /tmp/yq
        echo "${YQ_SHA256}  /tmp/yq" | sha256sum -c -
        install -m 755 /tmp/yq /usr/local/bin/yq
        rm -f /tmp/yq
    fi
    yq --version

    # sonar-scanner — needed by sonar.sh for SonarQube analysis
    if ! command -v sonar-scanner >/dev/null 2>&1; then
        SONAR_SCANNER_VERSION="6.2.0.4584"  # NOSONAR
        echo "Installing sonar-scanner ${SONAR_SCANNER_VERSION}"
        curl "${CURL_HTTPS[@]}" \
            "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SONAR_SCANNER_VERSION}-linux-x64.zip" \
            -o /tmp/sonar-scanner.zip
        unzip -q /tmp/sonar-scanner.zip -d "${HOME}/.sonar/"
        rm /tmp/sonar-scanner.zip
        ln -sf "${HOME}/.sonar/sonar-scanner-${SONAR_SCANNER_VERSION}-linux-x64/bin/sonar-scanner" /usr/local/bin/sonar-scanner
    fi
    sonar-scanner --version
fi

source .venv/bin/activate
configure_pip
# constraints.txt pins the full transitive graph sagemaker's extras pull in (mlflow,
# torch, pyiceberg, databricks-sdk, ...): most of those ~190 packages declare
# lower-bound-only ranges, so without this pip backtracks through dozens of releases
# of whichever one it tries next on every install. See constraints.txt for how to
# regenerate it when sagemaker/mlflow/torch bump.
pip install -r ./cdk_pipeline/requirements.txt -c ./cdk_pipeline/constraints.txt --upgrade --force-reinstall
pip install -r ./cdk_pipeline/requirements-dev.txt -c ./cdk_pipeline/constraints.txt --upgrade --force-reinstall

# Report results
echo "AWS CDK version"
cdk --version
python --version
