# Runbook — Feature Ingestion pipeline (NTU-927)

Onboard a feature group from an intake YAML in the sample-models repo, including any local transforms it brings

---

## 0. Set up your environment (first time only)

Every command below runs from the directory that holds this repo, so clone it beside the
sample-models repo you clone in step 1:

```bash
cd ~ && git clone https://bitbucketenterprise.aws.novartis.net/scm/pwsa0002037/pwsa0002037_novaos_ar.git
```

The scripts need Python >= 3.10 with `boto3`, `pandas`, `PyYAML`, `pyarrow` and the `aws`
CLI. One script installs all of them into a virtual environment:

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

## 1. Clone your Model YAMLs repo (e.g., sample-models repo)

```bash
export MODEL_REPO=pwsa0002037_novoas_ar_sample_models
export MODEL_REPO=pwsa0002037_novaos_ppm
export MODEL_REPO=pwsa0002037_novaos_kda

cd ~ && git clone https://bitbucketenterprise.aws.novartis.net/scm/pwsa0002037/$MODEL_REPO.git
```

Skip if already cloned; `git fetch` instead.

## 2. Check out to develop branch

```bash
export MODEL_REPO_BRANCH=develop
cd ~/$MODEL_REPO && git fetch && git checkout $MODEL_REPO_BRANCH
```

## 3. Select your feature group

Every step below reads this one variable — the intake directory of the feature group you
are onboarding. It lives at `<brand>/<model>/feature_intake/<feature_group>/`.

```bash
export INTAKE_DIR=<path to your feature group's intake directory>
```

Example:

```bash
export BRAND=brand_abc
export MODEL_NAME=patient_outcome_mortality_v2
export FEATURE_GROUP_NAME=demo_brandabc_cardiology_patient_outcome_metrics_v2

# pwsa0002037_novaos_kda/cosentyx/kda_cosentyx_hs_hcp_naive_nbrx/feature_intake/imm_cos_hs_comm_rx_hcp_mn

export BRAND=cosentyx
export MODEL_NAME=kda_cosentyx_hs_hcp_naive_nbrx

export FEATURE_GROUP_NAME=imm_cos_hs_nps_nps_perf_hcp_mn
export FEATURE_GROUP_NAME=imm_cos_hs_mkt_acc_claims_hcp_mn
export FEATURE_GROUP_NAME=imm_cos_hs_comm_tgt_hcp_mn
export FEATURE_GROUP_NAME=imm_cos_hs_comm_rx_hcp_mn
export FEATURE_GROUP_NAME=imm_cos_hs_comm_fe_hcp_mn
export FEATURE_GROUP_NAME=imm_cos_hs_comm_digital_hcp_mn
export FEATURE_GROUP_NAME=imm_cos_hs_comm_claims_hcp_mn

export INTAKE_DIR=~/$MODEL_REPO/$BRAND/$MODEL_NAME/feature_intake/$FEATURE_GROUP_NAME
```

## 4. Validate the intake

Run this before building or uploading anything to validate your intake.

```bash
python3 ~/pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/validate_intake.py
```

Exit codes: `0` valid, `1` the form is invalid, `2` the paths could not be resolved.

## 5. Build and push your job's image

This pushes your job's image to ECR to be used by the pipeline

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/build_and_push.sh
```

Unset `INTAKE_DIR` for a plain platform build, with no feature group's local transforms
baked in.

## 6. Upload the intake to S3

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/upload_intake.sh
```

It uploads to the **config bucket** at: 
`s3://novartis-970547336770-dev-us-east-1-novos-ar-config/feature-intake/<brand>/<model_id>/<feature_group_name>/`

On success it prints the execution inputs for the next step.

## 7. Run the pipeline

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/run_pipeline.sh

# to DELETE the feature group and CREATE it again pass `--recreate` flag
~/pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/run_pipeline.sh --recreate
```

It runs the feature ingestion pipeline using your intake

On success it prints the `run_id`, the registered `feature_group_name`, the feature
count and the ingest job name

## 8. Check the report

Find the output in feature-ingestion bucket under this prefix:

```
<feature-group-name>/<run-id>/compile-output/compiled_intake.json
<feature-group-name>/<run-id>/processing-output/ingest_report_<timestamp>.json
```

```bash
ING=novartis-970547336770-dev-us-east-1-novos-ar-feature-ingestion
FG=ar-mvp-dev-fgr-$(basename $INTAKE_DIR | tr '_' '-')

aws s3 ls "s3://$ING/$FG/" --recursive | grep processing-output
aws s3 cp "s3://$ING/$FG/<run-id>/processing-output/ingest_report_<ts>.json" -
```

---

1. **Do not create a second column with your event-time name.**
2. **Format event time as `YYYY-MM-DDTHH:MM:SSZ`.**

## 9. Query the features back

`query_store.py` reads from online and offline

### Online store — the serving read

Use the `sample_record_id` from the ingest report to test online store

```bash
python3 ~/pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/query_store.py \
  --online --id <sample_record_id>
```

Add `--features age,is_male` to fetch only some.

**An empty result is normal, not an error.** The online store is written by
`PutRecord` at ingest and expires on the group's TTL

### Offline via Athena

`--athena` runs the equivalent SQL against the Glue table instead

```bash
python3 ~/pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/query_store.py --offline --athena
```

It prints raw rows and distinct ids

```bash
[rows]   2000 raw / 500 distinct patient_id / 500 after latest-per-id
[note]   raw exceeds latest-per-id: the store is append-only, so re-runs
         accumulate versions. Deduplicate on read.
```

This needs a **Lake Formation grant** on the table, which the SageMaker notebook role
does not have today; without it Athena fails with `Insufficient Lake Formation
permission(s): Required Describe`. Ask the platform team for the grant


## 10. Re-running and cleanup

**You can rerun the same form** unless the form changed in a conflicting way: feature definitions, record identifier,
event-time column, or online/offline configuration

**`--recreate` deletes the feature group and its tables.**

```bash
~/pwsa0002037_novaos_ar/docs/runbooks/feature-ingestion/run_pipeline.sh --recreate
```

It asks you to type the feature group name before starting anything, and it does nothing
unless the intake actually conflicts — it is not a way to force a reset of a group that
already matches
