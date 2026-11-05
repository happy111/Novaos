# Runbook — Model-Ready Dataset pipeline (NTU-1038)

Turn one cohort-label run plus a model's MRD intake YAML into a split, validated, versioned
training dataset

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
  export `AWS_PROFILE`, before step 6.

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

## 3. Select your model and its upstream run

This pipeline reads **two** variables. The MRD form says what to do to a labelled cohort; it does
not restate what that cohort is, so the cohort-label run it consumes is an input in its own right.

```bash
export INTAKE_DIR=<path to the model's model_ready_dataset directory>
export UPSTREAM=<the cohort-label run id it is built from>
```

Example:

```bash
export BRAND=cosentyx
export MODEL_NAME=kda_cosentyx_hs_hcp_naive_nbrx

export INTAKE_DIR=~/$MODEL_REPO/$BRAND/$MODEL_NAME/model_ready_dataset
export UPSTREAM=r65a506a6e2fc 
```

A form is located by **model alone**, one per model, at `<model>/model_ready_dataset/`.

`UPSTREAM` is the `run_id` the cohort-label runbook's step 7 printed. Every script expands it to
`s3://<cohort-label bucket>/<run id>/population-label/population_labeled.parquet`, so the bucket is
never typed; pass that full URI instead if the cohort lives somewhere else.

## 4. Validate the intake

Run this before building or uploading anything to validate your intake.

```bash
python3 ~/pwsa0002037_novaos_ar/docs/runbooks/model-ready-dataset/validate_intake.py
```

Exit codes: `0` valid, `1` the form is invalid, `2` the paths could not be resolved.

Validator **needs AWS credentials**: half the checks compare the
form against the upstream run's target type, label column, entity id and cohort location, all read
from that run's own compiled intake.

## 5. Build and push your job's image

This pushes your job's image to ECR to be used by the pipeline

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/model-ready-dataset/build_and_push.sh
```

One image for all three stages

Unset `INTAKE_DIR` for a plain platform build, with no model's local transformations baked in.

## 6. Upload the intake to S3

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/model-ready-dataset/upload_intake.sh
```

It uploads to the **config bucket** at `model-ready-dataset-intake/<model_id>/`

On success it prints the execution input for the next step.

## 7. Run the pipeline

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/model-ready-dataset/run_pipeline.sh
```

It runs the model-ready-dataset pipeline using your intake and the upstream cohort

On success it prints the `run_id`, the model name, the target type, the compiled intake URI and
each of the three job names with its status

```bash
run_pipeline.sh --upstream r2bb1b39024fb        # instead of exporting UPSTREAM
run_pipeline.sh --as-of 2026-09-15T00:00:00Z    # override the point-in-time cutoff
run_pipeline.sh --no-wait                       # start it and return immediately
run_pipeline.sh --run-id r9z8y7x6w5v4u          # pin the run id, ^r[a-z0-9]{12}$
```

Compile (Lambda), then three Processing jobs in series — pit-join, split, validate-and-write —
each polled every 30s.

## 8. Check the report

The compile output and the two scratch stages are under the run id in the **mrd-datasets** bucket;
the dataset itself and the validation report are at platform-composed locations:

```
<run-id>/compile-output/compiled_intake.json
<run-id>/compile-output/cohort_source.sql
<run-id>/pit-join/
<run-id>/splits/
<model_name>/<version>/                       the immutable dataset
<model_name>/latest/                          the alias every run refreshes
validation-reports/<run-id>/validation_report.json
```

```bash
MRD=novartis-970547336770-dev-us-east-1-novos-ar-mrd-datasets
RUN_ID=<the run id from the execution>

aws s3 ls "s3://$MRD/$RUN_ID/" --recursive
aws s3 cp "s3://$MRD/validation-reports/$RUN_ID/validation_report.json" -
aws s3 ls "s3://$MRD/<model_name>/latest/" --recursive
```

`latest/` is written after the versioned copy

## 9. Re-running and cleanup

**Re-running is always safe** and needs no cleanup. Each run mints a new run id, and the dataset is
versioned by compile timestamp — a second run against an unchanged cohort produces a new version
rather than replacing one.

**Passing an explicit `run_id` reuses that prefix** and overwrites its output. Only do that to
re-drive a run you are debugging.

Nothing is deleted by a run; old `<run-id>/` prefixes and old dataset versions accumulate
