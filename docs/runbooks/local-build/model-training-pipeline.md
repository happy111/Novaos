# Model Training Pipeline

End-to-end: build the model container images, push them to ECR, then launch the
pipeline (**preprocess → train → evaluate → bias → register**) via
`scripts/run_training_pipeline.py`. This is the AR-08 model-training pipeline
— a SageMaker Pipeline the DS onboards a model into by dropping a YAML,
no code change. Every command below has been run for real against this account,
using the current `model-training-config.yaml` shape (nested `identity`/`data`/
`compute`, derived image repos, an ordered `preprocessing` transform list).

---

## Current Execution Context

| Field | Value |
|---|---|
| **SageMaker Role** | `RRDRAR65_AWS_APP01_PWSA0002037` |
| **Role ARN** | `arn:aws:iam::970547336770:role/RRDRAR65_AWS_APP01_PWSA0002037` |
| **Account ID** | `970547336770` |
| **Region** | `us-east-1` |

---

## Dependencies on Prior Modules

This module consumes the versioned labeled dataset produced upstream by
cohort-and-label. The following must exist before running any step here:

- A labeled dataset at
  `s3://<mrd-bucket>/<identity.model_name>/<data.source.s3_prefix>/<version>/`
  with `train.parquet`, `validation.parquet`, `holdout.parquet`, and a valid
  `manifest.json`. `<mrd-bucket>` is `config.dataset_bucket_name()` —
  `novartis-{account_id}-{AR_ENVIRONMENT}-{region}-{AR_APP_PREFIX}-mrd-datasets`
  — a fixed platform naming convention, provisioned separately (CDK) from
  `AR_ARTIFACT_BUCKET`. The dataset NAME is **derived** from
  `identity.model_name` — it is not authored anywhere in the config, so the
  dataset must already sit under a prefix matching that exact name. The S3
  PREFIX itself is authored: `data.source.s3_prefix` in the YAML — omitted or
  blank, it defaults to `"mrd"`. `<version>` (`data.source.dataset_version`,
  default `"latest"`) is a **literal** folder name the platform writes/
  overwrites in place — not a timestamp to scan for.
- The DS-owned model folder (in the sample-models repo) containing
  `model-training-config.yaml` beside its own code tree: `algorithm/`
  (training) and `evaluation/`. Prep code is NOT part of that tree — the
  driver is platform-owned (`src/processing/training-prep/`) and the config's
  `preprocessing` list drives it, so a model authors transforms in YAML, not
  in a script. `local_transformations/preprocessing/` + `compute/preprocess/`
  appear only in a model that overrides the default (see step 1).

---

## Required IAM Permissions

The execution role needs: S3 read/write on `AR_ARTIFACT_BUCKET`; S3 read on
the MRD dataset bucket (`config.dataset_bucket_name()`, a separate bucket
provisioned by CDK — not `AR_ARTIFACT_BUCKET`); SageMaker
Training/Processing/Pipelines/Model-Registry/Model/Batch-Transform actions
(`Create*`/`Describe*`/`List*`/`Stop*`/`UpdateModelPackage`/`AddTags`); ECR
`CreateRepository`/push actions + `GetAuthorizationToken`; IAM `GetRole` +
`PassRole` (scoped to `iam:PassedToService: sagemaker.amazonaws.com`); and
CloudWatch Logs write access under `/aws/sagemaker/{TrainingJobs,ProcessingJobs}*`.

Separately, whoever runs `build_and_push.sh` (step 1 below) needs
`secretsmanager:GetSecretValue` on the `jfrog-token` secret — that read
happens on the caller's own machine at build time, not inside the execution
role's SageMaker jobs.

See the **Service Summary** at the end of this doc for which AWS service each
step touches and why — build the exact policy from that if provisioning IAM
from scratch.

---

## Environment Variables

`.env.example` ships with the real Dev-account values already in use for this
workload (no secrets — bucket names, ARNs, resource IDs only), so
`cp .env.example .env` gives you a working config with no edits needed.

| Variable | Default in `.env.example` | Purpose |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Region |
| `AR_ARTIFACT_BUCKET` | `sagemaker-us-east-1-970547336770` | Artifacts + report bucket (pipeline's own outputs — prep/eval/bias/model artifacts). The labeled dataset lives in a SEPARATE bucket, see below |
| `AR_ARTIFACT_PREFIX` | `model-training` | Prefix under the artifact bucket for this pipeline's own outputs |
| `AR_ENVIRONMENT` | `dev` | Variable component of the MRD dataset bucket's name — see below |
| `AR_APP_PREFIX` | `novos-ar` | Variable component of the MRD dataset bucket's name — see below |
| `AR_KMS_KEY_ARN` | *(empty → SSE-S3)* | CMK for artifact/volume encryption |
| `AR_EXECUTION_ROLE_ARN` | `arn:aws:iam::970547336770:role/RRDRAR65_AWS_APP01_PWSA0002037` | Training/pipeline execution role |
| `AR_DATAZONE_DOMAIN_ID` | `dzd-c3poc7rfn8nyw7` | Unified Studio DataZone domain (visibility tag) |
| `AR_DATAZONE_PROJECT_ID` | `do9axtshgp150n` | Unified Studio project (visibility tag) |
| `AR_DATAZONE_SCOPE_NAME` | *(empty → `dev`)* | Project scope (visibility tag) |

---

## Local Environment Setup

```bash
cd /home/sagemaker-user/pwsa0002037_novaos_ar
python3 -m venv .venv
source .venv/bin/activate
pip install sagemaker-core sagemaker-mlops sagemaker-train sagemaker-serve \
  boto3 PyYAML jsonschema
```

| Package | Provides |
|---|---|
| `sagemaker-core` | `sagemaker.core.*` — resources, shapes, workflow functions/params, Clarify |
| `sagemaker-mlops` | `sagemaker.mlops.*` — `Pipeline`, `ModelStep`, processing/training steps |
| `sagemaker-train` | `sagemaker.train.*` — training job configs |
| `sagemaker-serve` | `sagemaker.serve.*` — model/inference spec |
| `boto3` / `PyYAML` / `jsonschema` | AWS calls / parse config / validate against the JSON schema |

Docker must be available for the image build step below. In SageMaker Studio,
every `docker build`/`push` needs `--build-network sagemaker` (the local
daemon only builds on that network).
```bash
cp .env.example .env      # defaults already filled in
set -a; source .env; set +a
```

---

## Running the Pipeline

Assumes the labeled dataset has already been written to the MRD bucket at
`<identity.model_name>/<data.source.s3_prefix>/<version>/` (see
**Dependencies on Prior Modules** above).

### 1. Build + push the images

Requires read access to the `jfrog-token` secret in Secrets Manager (a
JFrog Artifactory user/token pair): every image build downloads the
`ar_transformations` distribution from JFrog and bakes it in, so
preprocessing/training/evaluation code can dispatch through it.

Point the builder at your model's own code tree and give it the image
prefix `identity.model_name` derives to (hyphenated). The model's code sits
beside the YAML in `algorithm/` and `evaluation/`, so those two stages take
a `--src-<stage>`. **Preprocessing normally takes nothing**: the prep driver
is platform-owned (`src/processing/training-prep/`) and is baked in
automatically, because the step's arguments and the `preprocessing[]` list
from the config drive it entirely — there is nothing per-model to author.
That driver is a thin adapter over `ar_transformations`'
`preprocessing.base.ModelPreprocessor`, so the prep image needs the
`ar_transformations` sdist the same way the train and evaluate images do;
`build_and_push.sh` stages it from JFrog for all three.

```bash
cd /home/sagemaker-user/pwsa0002037_novaos_ar/src/images/default_containers
M=<path to the models model_training/ folder>
BUILD_NETWORK=sagemaker bash build_and_push.sh \
  --src-training   $M/algorithm \
  --src-evaluation $M/evaluation \
  --repo-prefix <identity.model_name, hyphenated>- \
  --entry-args-evaluation "--emit-predictions --bias-attr '<facet column, or empty>'"
```

That builds all three images. This is the right command whether
`preprocessing` lists steps or is `[]` — the steps are data, read at run time
from the file the pipeline uploads, so they never change the image.

Produces `<repo-prefix>{preprocessing,training,evaluation}` — the config's
step image repos are DERIVED from `identity.model_name` (hyphenated), so
`--repo-prefix` must match that derivation exactly or the pipeline pulls an
image that was never pushed.

Add the preprocessing flags only for a model that genuinely owns its prep
stage — a hand-written driver, an extra system package, a pinned dependency
its transforms need. Each one overrides the platform default independently,
and a `--src-preprocessing` you pass must match whatever
`compute.preprocess.dockerfile` / `local_requirements` record in the config:

```bash
  --src-preprocessing          $M/local_transformations/preprocessing \
  --dockerfile-preprocessing   $M/compute/preprocess/Dockerfile \
  --requirements-preprocessing $M/compute/preprocess/local_requirements.txt \
```

### 2. Dry run

`.env` (sourced in **Local Environment Setup** above) already has
`AWS_REGION`, `AR_ARTIFACT_BUCKET`, `AR_ENVIRONMENT`, and `AR_APP_PREFIX` —
no extra exports needed:

```bash
cd /home/sagemaker-user/pwsa0002037_novaos_ar
python3 scripts/run_training_pipeline.py \
  --config <path to model-training-config.yaml> \
  --dry-run
```

Confirms the definition builds — no AWS resources are created.

### 3. Real run

Same command, drop `--dry-run`:

```bash
python3 scripts/run_training_pipeline.py --config <path to model-training-config.yaml>
```

Prints the execution ARN. Track it in Unified Studio → **Pipelines**.

---

## Results, Approval, and Deployment

### Results in Unified Studio

- **Pipelines** → execution `<model>-training` — the 5 steps go green.
- **Models** → the Model Package Group → the new version, registered
  `PendingManualApproval`; the model card shows the hyperparameters and the
  held-out metrics from the Evaluate step.

### Approve the model version

A version registers `PendingManualApproval`; it cannot be deployed until
Approved. Either flip it in Unified Studio (**Models** → the group → the
version → **Update status** → Approved), or by CLI:

```bash
aws sagemaker list-model-packages --model-package-group-name <group>   # get <version-arn>
aws sagemaker update-model-package \
  --model-package-arn <version-arn> \
  --model-approval-status Approved
```

### Create the deployable Model

A Batch Transform runs a *Model*, not a package. Turn the latest Approved
version of the group into one:

```bash
cd /home/sagemaker-user/pwsa0002037_novaos_ar
python3 scripts/create_model_from_package.py --model-name <model_id>
```

Prints the created Model name (`<group>-v<version>`). Idempotent: re-running
reuses the existing Model. Shows in Unified Studio's **Custom models →
Deployable** list only when `AR_DATAZONE_DOMAIN_ID` / `AR_DATAZONE_PROJECT_ID`
are set; otherwise it still exists — check classic Studio → **Models**.

### Run a batch transform

Point the Model at an S3 prefix of headerless CSV (one instance per line,
features in trained column order); scores land as one `<input-object>.out`
per input object under the output prefix. Include the Model name
(`<group>-v<version>`) in `--output` — it's the one thing that changes
between versions, so leaving it out means every version's scores land at the
same path and overwrite each other:

```bash
python3 scripts/submit_batch_job.py \
  --model-name <group>-v<version> \
  --input  s3://<bucket>/<population-prefix>/ \
  --output s3://<bucket>/<scores-prefix>/<group>-v<version>/
```

`--output` is optional and defaults to a `batch-output/` sibling of the
input — but that default has no version in it, so it silently overwrites the
previous run's scores. Pass `--output` explicitly, as above, whenever you
might re-run against a different version.

---

## Service Summary

| AWS Service | Why It's Needed |
|---|---|
| **Amazon STS** | Resolve account identity (image URI account-prefixing, execution role resolution) |
| **Amazon S3** | Read the labeled dataset (MRD bucket); write prep output, model artifacts, evaluation/bias reports (artifact bucket) |
| **AWS IAM** | `GetRole` (resolve canonical ARN), `PassRole` (pass to SageMaker) |
| **Amazon SageMaker** (Training) | Run the training job |
| **Amazon SageMaker** (Processing) | Run preprocess, evaluate, and Clarify bias-check jobs |
| **Amazon SageMaker** (Pipelines) | Compose all steps into one DAG; start/track executions |
| **Amazon SageMaker** (Model Registry) | Register the model package after training (`PendingManualApproval`) |
| **Amazon SageMaker** (Models / Batch Transform) | Create a deployable Model from an Approved package; run scoring jobs against it |
| **Amazon ECR** | Store the per-model preprocessing/training/evaluation container images |
| **Amazon CloudWatch Logs** | Training and processing job log output |
| **AWS Secrets Manager** | Read the `jfrog-token` secret at image build time, to download the `ar_transformations` distribution from JFrog Artifactory |
