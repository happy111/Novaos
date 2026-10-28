#!/bin/bash
#####################################################################
# NOVOS AR — SonarQube analysis
#####################################################################

set -e

source ./cdk_pipeline/scripts/common.sh
# --proto '=https' refuses a redirect that downgrades to cleartext; -f so a
# failed download does not leave a truncated zip for unzip to choke on.
curl --proto '=https' --tlsv1.2 --create-dirs -fsSLo $HOME/.sonar/sonar-scanner.zip \
    "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SONAR_SCANNER_VERSION}-linux-x64.zip"
unzip -o $HOME/.sonar/sonar-scanner.zip -d $HOME/.sonar/
echo "Running SonarQube analysis"

if [[ -z "${SONAR_TOKEN}" ]]; then
    echo "ERROR: SONAR_TOKEN is not set" >&2
    exit 1
fi

if [[ ! -f test_unit_coverage.xml ]]; then
    echo "WARNING: test_unit_coverage.xml not found - coverage will be missing."
fi

export PATH="${SONAR_SCANNER_HOME}/bin:${PATH}"

# Per-file rule suppressions, one multicriteria block per entry (the scanner keys
# them by the index in the `multicriteria` list, so each entry needs its own
# ruleKey+resourceKey pair). Built from SONAR_ISSUE_IGNORES in common.sh, which
# carries the per-entry justification — read it before adding anything here.
ISSUE_IGNORE_ARGS=()
if [[ ${#SONAR_ISSUE_IGNORES[@]} -gt 0 ]]; then
    criteria=()
    for i in "${!SONAR_ISSUE_IGNORES[@]}"; do
        entry="${SONAR_ISSUE_IGNORES[$i]}"
        rule="${entry%%|*}"
        path="${entry#*|}"
        if [[ -z "${rule}" || -z "${path}" || "${rule}" == "${entry}" ]]; then
            echo "ERROR: malformed SONAR_ISSUE_IGNORES entry: '${entry}'" >&2
            echo "       expected 'ruleKey|path'" >&2
            exit 1
        fi
        id="ignore$((i + 1))"
        criteria+=("${id}")
        ISSUE_IGNORE_ARGS+=(
            "-Dsonar.issue.ignore.multicriteria.${id}.ruleKey=${rule}"
            "-Dsonar.issue.ignore.multicriteria.${id}.resourceKey=${path}"
        )
        echo "  suppressing ${rule} on ${path}"
    done
    # The index list itself must come first; join on commas.
    ISSUE_IGNORE_ARGS=(
        "-Dsonar.issue.ignore.multicriteria=$(IFS=,; echo "${criteria[*]}")"
        "${ISSUE_IGNORE_ARGS[@]}"
    )
    echo "Suppressing ${#SONAR_ISSUE_IGNORES[@]} rule/path pair(s) (see common.sh for why)"
fi

echo "Running SonarQube scanner (branch: ${BRANCH_NAME})..."
sonar-scanner \
    -Dsonar.projectKey="${SONAR_PROJECT_KEY}" \
    -Dsonar.branch.name="${BRANCH_NAME}" \
    -Dsonar.host.url="${SONAR_HOST_URL}" \
    -Dsonar.token="${SONAR_TOKEN}" \
    -Dsonar.python.coverage.reportPaths=test_unit_coverage.xml \
    -Dsonar.exclusions="docs/**,samples/**,**/tests/**,**/__pycache__/**,Checkmarx/**,**/Checkmarx/**" \
    -Dsonar.cpd.exclusions="${SONAR_CPD_EXCLUSIONS}" \
    "${ISSUE_IGNORE_ARGS[@]}" \
    -Dsonar.qualitygate.wait=true

echo "SonarQube analysis completed"
