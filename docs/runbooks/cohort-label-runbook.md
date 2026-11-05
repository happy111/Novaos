# Runbook — Cohort and Label pipeline (NTU-1037)

Turn one model's cohort-label intake YAML in the sample-models repo into a labelled cohort dataset

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
cd ~/pwsa0002037_novaos_ar && docs/runbooks/setup_venv.sh
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

## 2. Check out to develop branch

```bash
export MODEL_REPO_BRANCH=develop

cd ~/$MODEL_REPO && git fetch && git checkout $MODEL_REPO_BRANCH
```

## 3. Select your model

Every step below reads this one variable — the cohort-label intake directory of the model you are
running. A form is located by **model alone**, one per model, at `<model>/cohort_label_intake/`.

```bash
export INTAKE_DIR=<path to the model's cohort_label_intake directory>
```

Example:

```bash
export BRAND=cosentyx
export MODEL_NAME=kda_cosentyx_hs_hcp_naive_nbrx

export INTAKE_DIR=~/$MODEL_REPO/$BRAND/$MODEL_NAME/cohort_label_intake
```

## 4. Validate the intake

Run this before building or uploading anything to validate your intake.

```bash
python3 ~/pwsa0002037_novaos_ar/docs/runbooks/cohort-label/validate_intake.py
```

Exit codes: `0` valid, `1` the form is invalid, `2` the paths could not be resolved.

## 5. Build and push your job's image

This pushes your job's image to ECR to be used by the pipeline

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/cohort-label/build_and_push.sh
```

Unset `INTAKE_DIR` for a plain platform build, with no model's local transformations baked in.

## 6. Upload the intake to S3

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/cohort-label/upload_intake.sh
```

It uploads to the **config bucket** at `cohort-label-intake/<model_id>/`

On success it prints the execution inputs for the next step.

## 7. Run the pipeline

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/cohort-label/run_pipeline.sh
```

It runs the cohort-label pipeline using your intake

On success it prints the `run_id`, the compiled intake URI, the output prefix and the
population + label job name

```bash
run_pipeline.sh --as-of 2026-09-15T00:00:00Z   # pin the windowing reference date
run_pipeline.sh --no-wait                      # start it and return immediately
run_pipeline.sh --run-id r9z8y7x6w5v4u         # pin the run id, ^r[a-z0-9]{12}$
```

Compile (Lambda, 2 min timeout), then the Processing job polled every 30s

## 8. Check the report

Both stages write to the cohort-label bucket, run id first:

```
<run-id>/compile-output/compiled_intake.json
<run-id>/compile-output/population_source.sql
<run-id>/population-label/population_labeled.parquet
<run-id>/population-label/cohort_label_report_<timestamp>.json
```

```bash
CL=novartis-970547336770-dev-us-east-1-novos-ar-cohort-label
RUN_ID=<the run id from the execution>

aws s3 ls "s3://$CL/$RUN_ID/" --recursive
aws s3 cp "s3://$CL/$RUN_ID/population-label/cohort_label_report_<ts>.json" -
```

## 9. Re-running and cleanup

**Re-running is always safe** and needs no cleanup. Each run mints a new run id, and
`storage.s3_uri` is versioned by compile timestamp — nothing is overwritten, no state carries over.

**Passing an explicit `run_id` reuses that prefix** and overwrites its output. Only do that to
re-drive a run you are debugging.

Nothing is deleted by a run; old `<run-id>/` prefixes accumulate
