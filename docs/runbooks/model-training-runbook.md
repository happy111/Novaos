# Runbook — Model Training pipeline (AR-08)

Turn one model's training config and its own code in the sample-models repo into a
registered model version (**preprocess → train → evaluate → bias → register**)

---

## 0. Set up your environment (first time only)

Every command below runs from the directory that holds this repo, so clone it beside the
sample-models repo you clone in step 1:

```bash
cd ~ && git clone https://bitbucketenterprise.aws.novartis.net/scm/pwsa0002037/pwsa0002037_novaos_ar.git
```

The scripts need Python >= 3.10 with `boto3`, `pandas`, `PyYAML`, `pyarrow` and the `aws`
CLI — which a machine outside a SageMaker notebook generally does not have. One script
installs all of them into a virtual environment:

```bash
cd ~/pwsa0002037_novaos_ar/docs/runbooks/setup_venv.sh
```

Activate it in **every** shell that runs a step below:

```bash
cd ~/pwsa0002037_novaos_ar && source .venv-runbooks/bin/activate
```

Re-run `setup_venv.sh` whenever you want to upgrade it in place. It also reports the two
things it cannot install for you:

- `docker` and `jq`, needed by step 5 alone — every other step works without them.
- AWS credentials, which are **not** ambient outside SageMaker. Run `aws configure`, or
  export `AWS_PROFILE`, before step 5. Step 4 runs without them but skips its dataset
  check, which is the one the pipeline most often fails on.

There is no `.env` to copy. Everything environment-specific — account, region, the
artifact bucket, the execution role, the CMK, the DataZone domain and project — is
already set on the two pipeline Lambdas from `cdk_pipeline/environments/<env>.json`, and
the scripts below read that same file for the bucket and state machine names. Nothing
account-specific is yours to fill in.

The one exception is the local `--dry-run` harness in step 7, which runs the orchestration
in your own shell instead of in the Lambdas and so needs those values in your environment.
Every numbered step below runs without it.

## 1. Clone the sample-models repo

```bash
export MODEL_REPO=pwsa0002037_novoas_ar_sample_models
export MODEL_REPO=pwsa0002037_novaos_kda

cd ~ && git clone https://bitbucketenterprise.aws.novartis.net/scm/pwsa0002037/$MODEL_REPO.git
```

Skip if already cloned; `git fetch` instead.

## 2. Check out to develop

```bash
export MODEL_REPO_BRANCH=develop

cd ~/$MODEL_REPO && git fetch && git checkout $MODEL_REPO_BRANCH
```

## 3. Select your model

Every step below reads this one variable — the model-training directory of the model you
are training. It holds `model-training-config.yaml` beside the model's own code, one per
model, at `<model>/model_training/`.

```bash
export INTAKE_DIR=<path to the model's model_training directory>
```

Example:

```bash
export BRAND=cosentyx
export MODEL_NAME=kda_cosentyx_hs_hcp_naive_nbrx

export INTAKE_DIR=~/$MODEL_REPO/$BRAND/$MODEL_NAME/model_training
```

The directory carries the config, the training code, and the evaluation code:

```
model-training-config.yaml
algorithm/train.py                        # algorithm.source_script
evaluation/eval.py                        # evaluation.source_script
compute/<step>/Dockerfile                 # OPTIONAL. compute.<step>.dockerfile
compute/<step>/local_requirements.txt     # OPTIONAL. compute.<step>.local_requirements
local_transformations/preprocessing/      # OPTIONAL. only a model overriding platform prep
```

Preprocessing normally ships nothing: the prep driver is platform-owned
(`src/processing/training-prep/`), and the config's ordered `preprocessing` list drives it
entirely, so there is nothing per-model to author.

## 4. Validate the config

Run this before building or uploading anything to validate your config.

```bash
python3 ~/pwsa0002037_novaos_ar/docs/runbooks/model-training/validate_config.py
```

Exit codes: `0` valid, `1` the config is invalid, `2` the paths could not be resolved.

It runs the same checks the pipeline's Validate state runs, so a config that fails here
would have failed there. It also prints the three image refs the next step pushes to, so
you can see what will be built before building it.

The last of the three checks — that the labeled dataset resolves to a real version in S3
— needs AWS credentials. Without them it is **skipped with a warning** and the exit code
is still `0`, so read the output rather than only the exit code.

The onboarding form is picked up automatically from the sibling `model_onboarding/`
directory when it holds one, which also checks the training gate against the approved
success criteria. Point at another with `--onboarding-form <path>`.

## 5. Build and push your job's images

This pushes your model's three stage images to ECR to be used by the pipeline

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/model-training/build_and_push.sh
```

One command builds all three — `preprocessing`, `training`, `evaluation`. Every argument
comes from the config: the source trees from `algorithm.source_script` and
`evaluation.source_script`, the ECR repo names from `identity.model_name`, the compute
flavour from `compute.train.instance_type`, the evaluation entry arguments from
`evaluation.run_bias_check` and `bias_config.facet_columns`, and any per-step Dockerfile
or extra requirements from `compute.<step>`. Nothing platform-specific is passed by hand.

Build one stage or a subset by naming them, and check the derivation before building:

```bash
build_and_push.sh training                  # one stage
build_and_push.sh preprocessing training    # a subset
build_and_push.sh --dry-run                 # print the composed builder call, build nothing
build_and_push.sh --compute gpu             # override what the instance type implies
```

A path in the config resolves against three roots, tried in order — the config's own
directory, the models repo root, and this repo's `src/images/` — because all three are in
use across the sample models. A `compute.<step>.dockerfile` that resolves under
`src/images/` is naming the **platform's** own Dockerfile for that stage, which the
builder already builds from, so it is reported and not passed as an override.

The repos are **per-model** — `<model-name-hyphenated>-{preprocessing,training,evaluation}`
— derived from `identity.model_name` by the same function the pipeline resolves each
step's image with, so what is pushed here is what the run pulls. Rebuild whenever that
model's code, Dockerfile, or `local_requirements` change; the config's `preprocessing`
list is data read at run time and never changes an image.

Requires read access to the `jfrog-token` secret in Secrets Manager: every image bakes in
the `ar_transformations` distribution, which the baked code dispatches through. That read
happens on your own machine at build time, not inside the pipeline's jobs.

## 6. Upload the config to S3

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/model-training/upload_config.sh
```

It uploads to the **model-training bucket** at `configs/<model_id>/`

On success it prints the execution inputs for the next step.

The onboarding form goes up alongside the config, taken from the sibling
`model_onboarding/` directory when it holds one (`--onboarding-form <path>` for another).
Without one the registered version gets no Model Card and the training gate is not
checked against the approved criteria, so the script says so rather than passing quietly.

**Not the shared config bucket** the other three pipelines upload to, which is the one
place this runbook cannot mirror them. Both pipeline Lambdas run as the generic
`novos-ar-lambda-execution` role, whose only S3 grant is the model-training bucket
(`cdk_iam_deploy_roles.py`, sid `S3ModelTrainingArtifacts`); a config in the config
bucket is read with a `403` at the first state. Copies do sit in the config bucket under
`model-training/` from an earlier attempt — nothing can read them. Putting configs in
the config bucket instead would take one added S3 statement on that role.

Within this bucket the prefix is `configs/`, not `model-training/`: the latter is
`AR_ARTIFACT_PREFIX`, where the pipeline writes its own per-run outputs (step 8), and a
config is an input.

The prefix is not load-bearing — the state machine is handed the config's full S3 URI
rather than composing a path from a model id — but keeping the convention means step 7
can derive that URI instead of asking you for it.

## 7. Run the pipeline

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/model-training/run_pipeline.sh
```

It runs the model-training pipeline using your config

On success it prints the SageMaker Pipeline name, the pipeline execution ARN and its
terminal status

```bash
run_pipeline.sh --no-wait                        # start it and return immediately
run_pipeline.sh --no-onboarding-form             # start without seeding the Model Card
run_pipeline.sh --onboarding-form-uri s3://...   # use a form other than the uploaded one
```

It refuses to start if no config is at the derived S3 URI: the run reads S3, not your
working tree, so an un-uploaded edit would otherwise run silently against the old config.
The onboarding form is used automatically when step 6 put one there.

Validate (Lambda, 5 min timeout), then Build & Launch (Lambda, 10 min timeout), then the
SageMaker Pipeline execution polled every 60s. The five steps run inside that execution —
the state machine tracks it as one unit, so a step failure surfaces as a failed pipeline
execution, not a failed state, and is read in Studio → **Pipelines** rather than in the
state machine's own history.

For a dry run — validate and build the definition without creating anything — use the
local harness instead. It is the one path that needs the environment in your own shell:

```bash
set -a; source .env; set +a
python3 ~/pwsa0002037_novaos_ar/scripts/run_training_pipeline.py --config $INTAKE_DIR/model-training-config.yaml --dry-run
```

## 8. Check the results

The pipeline writes under the model-training bucket, model first, then the execution id:

```
model-training/<model_name>/<execution-id>/prep/        # fitted transformers, .npy arrays, split_test.parquet
model-training/<model_name>/<execution-id>/output/      # model.tar.gz
model-training/<model_name>/<execution-id>/evaluation/  # metrics.json, predictions.csv
model-training/<model_name>/<execution-id>/bias/        # Clarify analysis.json + report
```

`<model_name>` is the config's `identity.model_name` **as written** — underscores and
all. Only the MRD dataset folder read in step 4 is hyphenated, because M4 slugs what it
writes; the artifacts this pipeline writes are not slugged.

```bash
MT=novartis-970547336770-dev-us-east-1-novos-ar-model-training
MODEL=<identity.model_name>

aws s3 ls "s3://$MT/model-training/$MODEL/" --recursive
aws s3 cp "s3://$MT/model-training/$MODEL/<execution-id>/evaluation/metrics.json" -
```

In Unified Studio:

- **Pipelines** → execution `<model>-training` — the five steps go green.
- **Models** → the Model Package Group → the new version, registered
  `PendingManualApproval`; the model card shows the hyperparameters and the held-out
  metrics from the Evaluate step.

Evaluate and the bias check are **record-only**. They write metrics and never gate
registration, so a version registers even when a threshold is missed — read
`metrics.json` before approving, do not infer it from the pipeline succeeding.

Read the two bias metric families as a pair: pre-training measures skew in the data,
post-training skew in the model. A post-training gap matching the pre-training gap means
the model reproduced an existing imbalance; a gap that widens means it amplified one.

---

## 9. Approve and deploy

A version registers `PendingManualApproval` and cannot be deployed until Approved.
Either flip it in Unified Studio (**Models** → the group → the version → **Update
status** → Approved), or by CLI:

```bash
aws sagemaker list-model-packages --model-package-group-name <group>   # get <version-arn>
aws sagemaker update-model-package \
  --model-package-arn <version-arn> \
  --model-approval-status Approved
```

### Create the deployable Model

A Batch Transform runs a *Model*, not a package. Turn the latest Approved version of the
group into one:

```bash
python3 pwsa0002037_novaos_ar/scripts/create_model_from_package.py --model-name <model_id>
```

Prints the created Model name (`<group>-v<version>`). Idempotent: re-running reuses the
existing Model.

### Run a batch transform

Scores land as one `<input-object>.out` per input object under the output prefix. Which
input shape to use depends on whether the model's `inference.py` reads a header:

**Headerless (default, `--split-type Line`)** — a model whose serving contract is a bare
positional CSV (features in trained column order, nothing else). SageMaker splits by
newline into mini-batches, so one large file is fine:

```bash
python3 pwsa0002037_novaos_ar/scripts/submit_batch_job.py \
  --model-name <group>-v<version> \
  --input  s3://<bucket>/<population-prefix>/ \
  --output s3://<bucket>/<scores-prefix>/<group>-v<version>/
```

**Headered (`--split-type None`)** — a model whose `inference.py` subclasses
`ar_transformations.inference.base.ModelServer` and expects a model-ready-dataset extract
posted as it stands (patient id, index date, label and all, selected down to the feature
columns by NAME). A header travels with line-split's first mini-batch only, so every later
one arrives headerless and fails — `--split-type None` delivers each S3 object whole
instead, which means the INPUT PREFIX must be several self-contained files (each its own
header + rows), every one under the account's Batch Transform payload ceiling (SageMaker
still enforces it even unsplit — "deliver whole" is not "ignore size"):

```bash
python3 pwsa0002037_novaos_ar/scripts/submit_batch_job.py \
  --model-name <group>-v<version> \
  --input  s3://<bucket>/<population-prefix>/           # several headered files, not one big one
  --output s3://<bucket>/<scores-prefix>/<group>-v<version>/ \
  --split-type None
```

**Pass `--output` explicitly.** It defaults to a `batch-output/` sibling of the input,
and that default carries no version, so every version's scores land at the same path and
overwrite each other.

### A model needing a dependency the shared inference image lacks

The two platform inference images (`job-inference-cpu`/`-gpu`) are model-agnostic by
default and serve every model with no per-model build — but they only carry the deps their
own `requirements.txt` lists (torch, scikit-learn, pandas, pyarrow, pyyaml, gunicorn,
flask). A model whose `inference.py` imports something else (xgboost is the first case:
`joblib.load()`-ing an `XGBClassifier` needs the class importable, which fails with
`ModuleNotFoundError` otherwise) authors `compute.inference.local_requirements` — exactly
the same field train/evaluate already use — pointing at a `requirements.txt` carrying the
extra dep:

```yaml
compute:
  inference:
    local_requirements: "compute/inference/local_requirements.txt"
    instance_type: "ml.m5.large"
```

That one field is the signal the register step uses to switch from the two shared images
onto a per-model one — build it explicitly (it is NOT in the default three-stage build):

```bash
pwsa0002037_novaos_ar/docs/runbooks/model-training/build_and_push.sh inference
```

A model that authors neither `local_requirements` nor `dockerfile` under
`compute.inference` is unaffected by any of this — it keeps resolving the same two shared
images as every model always has.

## 10. Re-running and cleanup

**Re-running is always safe** and needs no cleanup. Each run mints a new pipeline
execution id and every artifact is written under it, so nothing is overwritten and no
state carries over. The definition is upserted each run — an edited config replaces the
definition rather than accumulating one.

**Each run registers a new model version** in the group, `PendingManualApproval`.
Re-running does not supersede or delete the previous version; versions accumulate and are
approved individually.

**Nothing is deleted by a run.** Old `<execution-id>/` prefixes accumulate.

Rebuild the images (step 5) before re-running if the model's code changed — the pipeline
resolves `<model>-<stage>:latest` and pulls whatever that tag points at now, so a re-run
against an unchanged tag silently trains the old code.

## Dependencies on upstream modules

The labeled dataset from the model-ready-dataset pipeline must already exist at

```
s3://novartis-970547336770-dev-us-east-1-novos-ar-mrd-datasets/<identity.model_name>/<data.source.s3_prefix>/<version>/
```

with `train.parquet`, `validation.parquet`, `holdout.parquet` and a valid
`manifest.json`. Three things about that path are worth knowing before you author the
config:

- The **bucket** is a fixed platform naming convention, derived — not authored anywhere.
- The **dataset name** is derived from `identity.model_name`, also not authored, so the
  dataset must already sit under a prefix matching that exact name.
- Only the **prefix** and **version** are yours: `data.source.s3_prefix` (blank defaults
  to `mrd`) and `data.source.dataset_version` (blank defaults to `latest`). Both are
  literal folder names the platform writes in place — `latest` is not a timestamp to
  scan for.

Step 4 fails with the resolved path when that folder has no `manifest.json`, which is the
cheapest way to find out the upstream run has not landed.

## Service Summary

| AWS Service | Why It's Needed |
|---|---|
| **AWS Step Functions** | Run the three-state workflow: Validate, Build & Launch, poll the pipeline |
| **AWS Lambda** | The Validate and Build & Launch states |
| **Amazon S3** | Read the labeled dataset (MRD bucket) and the committed config (config bucket); write prep output, model artifacts, evaluation and bias reports (model-training bucket) |
| **Amazon SageMaker** (Pipelines) | Compose the five steps into one DAG; start and track executions |
| **Amazon SageMaker** (Training) | Run the training job |
| **Amazon SageMaker** (Processing) | Run preprocess, evaluate, and Clarify bias-check jobs |
| **Amazon SageMaker** (Model Registry) | Register the model package after training (`PendingManualApproval`) |
| **Amazon SageMaker** (Models / Batch Transform) | Create a deployable Model from an Approved package; run scoring against it |
| **Amazon ECR** | Store the per-model preprocessing, training and evaluation images |
| **AWS KMS** | Wrap artifacts and job volumes with the platform CMK |
| **Amazon CloudWatch Logs** | Training and processing job log output |
| **AWS Secrets Manager** | Read the `jfrog-token` secret at image build time, to download `ar_transformations` from JFrog Artifactory |
| **AWS IAM** | `PassRole` to SageMaker for the pipeline execution role |
