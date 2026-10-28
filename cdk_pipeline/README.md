# pwsa0002037_novaos_ar — Analytics Rewiring (AR) Platform

AWS platform code for Novartis' Analytics Rewiring (AR) initiative: Feature Store
onboarding, cohort/label engineering, model training orchestration, model
registry/approval, serving, monitoring, and metadata (Neptune) integration for
KDA/PPM data-science models. Deployed via AWS CDK (Python) across `dev`, `qa`, and
`prod` workload accounts.

See [`docs/architecture/account-topology.md`](docs/architecture/account-topology.md)
for the full account inventory and cross-account access contract, and
[`docs/milestones/SOW-AR-Milestones.md`](docs/milestones/SOW-AR-Milestones.md) for
the delivery roadmap (M1–M6, July–Dec 2026).

---

## Module map (M1–M9)

Each module has a low-level design under
[`docs/lld/buildable-modules/`](docs/lld/buildable-modules/):

| Module | Design doc | What it covers |
|---|---|---|
| M1 | [ML Development Environment](docs/lld/buildable-modules/M1-ml-development-environment.md) | SageMaker Unified Studio, IAM Identity Center federation, Dev/QA/Prod training execution |
| M2 | [Feature Store](docs/lld/buildable-modules/M2-feature-store.md) | YAML intake onboarding, Snowflake-sourced feature groups, online/offline stores |
| M3 | [Feature Refresh Pipeline](docs/lld/buildable-modules/M3-feature-refresh-pipeline.md) | Scheduled feature refresh and data-freshness gating |
| M4 | [Cohort and Label Engineering](docs/lld/buildable-modules/M4-cohort-and-label-engineering.md) | Cohort/label intake, point-in-time-correct dataset join |
| M5 | [Model Training & Pipeline Orchestration](docs/lld/buildable-modules/M5-model-training-orchestration.md) | Extract → features → train → evaluate → register pipeline |
| M6 | [Model Registry & Approval Workflow](docs/lld/buildable-modules/M6-model-registry-approval.md) | SageMaker Model Registry, HITL approval, promotion to Prod |
| M7 | [Model Serving & MCP Integration](docs/lld/buildable-modules/M7-model-serving-mcp.md) | SageMaker endpoints, agent-callable MCP tools |
| M8 | [Model Monitoring & Drift Detection](docs/lld/buildable-modules/M8-model-monitoring-drift.md) | Daily drift checks, retraining triggers |
| M9 | [ML Metadata Integration](docs/lld/buildable-modules/M9-ml-metadata-integration.md) | Neptune graph lineage (features, models, KDA insights), Collate catalog sync |

Corresponding runbooks (step-by-step operator instructions) live in
[`docs/runbooks/`](docs/runbooks/):
[feature ingestion](docs/runbooks/feature-ingestion-runbook.md),
[cohort and label](docs/runbooks/cohort-label-runbook.md),
[model-ready dataset](docs/runbooks/model-ready-dataset-runbook.md), and
[model training](docs/runbooks/model-training-runbook.md).

---

## Repository layout

```
cdk_pipeline/      AWS CDK (Python) app — stacks, environments/, tests
src/
  images/          Dockerfiles + requirements.txt per container (base + job images)
  lambda/          Lambda functions (validation, ingestion, pipeline launch/validate)
  pipelines/       Pipeline orchestration logic shared by Lambdas and dev CLIs
  common/          Shared helpers used across jobs/lambdas
  model_training_core/   DS-facing training/evaluation base classes
docs/
  architecture/    Account topology, cross-account governance
  lld/             Per-module low-level designs (M1-M9) + YAML intake templates
  onboarding/      DS team onboarding contract (YAML intake form conventions)
  runbooks/        Operator runbooks + their own requirements.txt/setup_venv.sh
  milestones/      SOW milestone plan
  sprint/          Jira/epics tracking notes
samples/           Standalone AWS-connectivity validation scripts (not for prod use)
schemas/           JSON Schemas for YAML intake forms
scripts/           Dev-only CLIs (run_training_pipeline.py, submit_batch_job.py, ...)
templates/         Example filled-in YAML intake forms
tests/             Unit tests (pytest) + fixtures
```

---

## Setup

Prerequisites: Python ≥ 3.10 (per `pyproject.toml`), AWS CDK CLI ≥ 2.150.0, Node.js
≥ 18.x (required by the CDK CLI), and the `aws` CLI.

```bash
git clone https://bitbucketenterprise.aws.novartis.net/scm/pwsa0002037/pwsa0002037_novaos_ar.git
cd pwsa0002037_novaos_ar

python3 -m venv .venv
source .venv/bin/activate
pip install -r cdk_pipeline/requirements.txt
pip install -r cdk_pipeline/requirements-dev.txt   # for local testing/linting

cdk bootstrap aws://<account-id>/us-east-1 --context environment=dev   # first time only, per account/region
```

For the operator runbooks (feature ingestion, cohort/label, model-ready-dataset,
model-training), use the lighter environment described in each runbook, built with
[`docs/runbooks/setup_venv.sh`](docs/runbooks/setup_venv.sh) instead — it installs
only what the runbook scripts import, not the CDK/test toolchain above.

---

## Build / Deploy

The CDK app ([`cdk_pipeline/app.py`](cdk_pipeline/app.py)) instantiates all stacks
(IAM roles, S3 buckets, notifications, Lambdas, and the feature-onboarding,
cohort-label, model-ready-dataset, and model-training state machines) from the
JSON config under [`cdk_pipeline/environments/`](cdk_pipeline/environments/)
(`dev.json` / `qa.json` / `prod.json`), selected via `--context environment=<env>`.

Via the helper scripts (used by CI, see [`Jenkinsfiles/`](Jenkinsfiles/)):

```bash
./cdk_pipeline/scripts/build.sh     # cdk synth
./cdk_pipeline/scripts/deploy.sh    # cdk deploy --all (or a comma-separated subset of stack names as $1)
```

Or directly with the CDK CLI:

```bash
cd cdk_pipeline
cdk synth --context environment=dev
cdk diff --context environment=dev
cdk deploy --all --context environment=dev
```

Container images (base + per-job) are built and pushed separately — see
[`src/images/build_and_push.sh`](src/images/build_and_push.sh) and
[`cdk_pipeline/scripts/build-src-images.sh`](cdk_pipeline/scripts/build-src-images.sh).

---

## Tests

```bash
./cdk_pipeline/scripts/test.sh
```

which runs, outside CI:

```bash
pytest -svvv ./cdk_pipeline/tests --cov=./cdk_pipeline --cov-report=term-missing
```

Lint/security checks (`black`, `isort`, `ruff`, `bandit`, `pip-audit`,
`detect-secrets`) are configured in [`pyproject.toml`](pyproject.toml) and run via
[`cdk_pipeline/scripts/linting.sh`](cdk_pipeline/scripts/linting.sh) and
[`cdk_pipeline/scripts/security.sh`](cdk_pipeline/scripts/security.sh).

---

## DS team onboarding

Data scientists onboarding a new model or feature group do not work in this repo —
see [`docs/onboarding/README_DS_Onboarding.md`](docs/onboarding/README_DS_Onboarding.md)
for the YAML intake-form contract and the expected structure of a DS team's own
Bitbucket repo.

---

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md).
