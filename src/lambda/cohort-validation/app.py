"""Cohort-label intake compiler — stage 1 of the cohort-label state machine.

Validates the intake form against src/common/cohort_label_schema.py.

Reads the cohort-label intake directory a DS staged in the config bucket, validates
the form, and writes under `[Run ID]/compile-output/` in the cohort-label bucket:

    compiled_intake.json   the contract the population + label job reads
    population_source.sql  the population query with its ${...} placeholders bound
    label_source.sql       the label query, when the form declares a different one

Then it returns the compiled intake's S3 URI plus its own inputs, echoed forward.

Event — the execution input, whole, plus what the state machine minted:
    {
      "model_id":                     "cosentyx/patient_outcome_mortality",
      "feature_group_name":           "demo_cosentyx_hs_switch_quick_features",  # optional
      "cohort_label_intake_s3_uri":   "s3://.../cohort-label-intake/<brand>/<model>/",
      "run_id":                       "r5f3a9c2d1e0b",          # optional, see below
      "platform": {"generated_run_id": "r5f3a9c2d1e0b"},        # minted by the ASL
      "as_of":                        "2026-09-15T00:00:00Z",   # optional, defaults to now
      "image_tag":                    "novos-ar-job-cohort-label-...",  # optional
      "dry_run":                      false                      # optional
    }

A cohort-label form is located by MODEL alone — one form per model, under
`cohort-label-intake/<brand>/<model>/`. `feature_group_name` is not a path segment
and is carried forward for linkage only.

The intake directory is staged to S3 manually, as in the feature-ingestion runbook:
the form, its `sql/` sibling, and any `local_transformations/` module. It is mirrored
into the Lambda's own /tmp so the form's relative paths resolve the way they do on
the author's machine and in the container's code channel.

A malformed form raises ValidationError listing every offending field, and nothing
is written. `dry_run` compiles and returns the document without writing it.
"""

import json
import os
import shutil
import tempfile

import boto3
import yaml

from src.common import config as config_module
from src.common import intake_schema
from src.common.cohort_label_compiler import (
    compile_intake,
    compiled_intake_key,
)
from src.common.cohort_label_schema import FORM_FILENAME, validate_or_raise

# The prefix a team's cohort-label intake directory sits under in the config bucket.
COHORT_LABEL_INTAKE_PREFIX = "cohort-label-intake"
# Where the state machine puts what it minted, beside the caller's own input.
PLATFORM_KEY = "platform"
GENERATED_RUN_ID_KEY = "generated_run_id"


class EventError(Exception):
    """The invocation event is missing or malformed."""


def _log(message, **fields):
    """Structured log line. runId is on every line, per X5."""
    print(json.dumps({"message": message, **fields}))


def _split_s3_uri(uri, field):
    if not str(uri or "").startswith("s3://"):
        raise EventError(f"{field}: {uri!r} must be an s3:// URI.")
    bucket, _, key = uri[len("s3://") :].partition("/")
    if not bucket or not key:
        raise EventError(f"{field}: {uri!r} must include a bucket and a key.")
    return bucket, key


def _run_id(event):
    """The caller's run id, else the one the state machine minted for this run."""
    supplied = str(event.get("run_id") or "").strip()
    if supplied:
        return supplied
    platform = event.get(PLATFORM_KEY) or {}
    return str(platform.get(GENERATED_RUN_ID_KEY) or "").strip()


def _intake_uri(event, config_bucket):
    """The intake directory: named outright, or composed from the model id."""
    given = str(event.get("cohort_label_intake_s3_uri") or "").strip()
    if given:
        return given
    model_id = str(event.get("model_id") or "").strip().strip("/")
    if not model_id:
        raise EventError(
            "the execution input must carry either cohort_label_intake_s3_uri, or "
            "model_id as '<brand>/<model>'."
        )
    return f"s3://{config_bucket}/{COHORT_LABEL_INTAKE_PREFIX}/{model_id}/"


def _parse_event(event, config_bucket):
    run_id = _run_id(event)
    if not intake_schema.is_valid_run_id(run_id):
        raise EventError(
            f"run_id: {run_id!r} must be 'r' followed by 12 lowercase base32 "
            "characters, e.g. r5f3a9c2d1e0b."
        )
    return run_id, _intake_uri(event, config_bucket)


def download_intake_dir(s3, intake_s3_uri, account_id, destination):
    """Mirror the staged intake directory into `destination`, keeping its layout.

    A fresh directory per invocation: /tmp survives a warm Lambda, so a file left by
    an earlier run must not be mistaken for one this form staged.
    """
    bucket, prefix = _split_s3_uri(
        intake_s3_uri.rstrip("/") + "/_", "cohort_label_intake_s3_uri"
    )
    prefix = prefix[: -len("_")]

    downloaded = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix=prefix, ExpectedBucketOwner=account_id
    ):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith("/"):
                continue
            relative = key[len(prefix) :]
            target = os.path.join(destination, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            s3.download_file(bucket, key, target)
            downloaded.append(relative)

    if not downloaded:
        raise EventError(
            f"cohort_label_intake_s3_uri: {intake_s3_uri} is empty or does not exist."
        )
    if FORM_FILENAME not in downloaded:
        raise EventError(
            f"cohort_label_intake_s3_uri: no {FORM_FILENAME} directly under "
            f"{intake_s3_uri}. Staged: {sorted(downloaded)}"
        )
    return downloaded


def _put(s3, cfg, key, body, content_type):
    s3.put_object(
        Bucket=cfg.cohort_label_bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType=content_type,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=cfg.kms_key_arn,
        ExpectedBucketOwner=cfg.account_id,
    )


def handler(event, context):
    cfg = config_module.load_cohort_label()
    run_id, intake_s3_uri = _parse_event(event, cfg.config_bucket)
    s3 = boto3.client("s3")

    intake_dir = tempfile.mkdtemp(prefix=f"{run_id}-", dir="/tmp")  # NOSONAR
    try:
        staged = download_intake_dir(s3, intake_s3_uri, cfg.account_id, intake_dir)
        _log(
            "compiling cohort-label intake",
            runId=run_id,
            cohortLabelIntakeDir=intake_s3_uri,
            stagedObjects=sorted(staged),
        )

        with open(os.path.join(intake_dir, FORM_FILENAME)) as handle:
            form = yaml.safe_load(handle)

        # Field-level validation before anything expensive happens: an unrecognized
        # population.strategy or label.target_type is rejected here, before the
        # Processing job and the Snowflake read (AR-18-a-2 AC-3/AC-4).
        warnings = validate_or_raise(form, intake_dir)
        for warning in warnings:
            _log(
                "validation warning",
                runId=run_id,
                field=warning.field,
                detail=warning.message,
            )

        identity = {
            "model_id": event.get("model_id"),
            "feature_group_name": event.get("feature_group_name"),
            "intake_s3_uri": intake_s3_uri,
            # Absent — the normal case — the image is derived from model_id.
            "image_tag": str(event.get("image_tag") or "").strip() or None,
        }
        compiled, objects = compile_intake(
            cfg, form, intake_dir, identity, run_id, as_of=event.get("as_of")
        )
    finally:
        shutil.rmtree(intake_dir, ignore_errors=True)

    key = compiled_intake_key(run_id)
    uri = f"s3://{cfg.cohort_label_bucket}/{key}"

    result = {
        "status": "ok",
        "run_id": run_id,
        # The compiler's own inputs, echoed forward for the next stage.
        "model_id": compiled["model_id"],
        "feature_group_name": compiled["feature_group_name"],
        "cohort_label_intake_s3_uri": intake_s3_uri,
        "compiled_intake_s3_uri": uri,
        "model_name": compiled["model_name"],
        "target_type": compiled["label"]["target_type"],
        # Needed by stage 2.
        "processing_job_name": compiled["job"]["processing_job_name"],
        "compute": compiled["compute"],
        "role_arn": compiled["job"]["role_arn"],
        "output_s3_uri": compiled["job"]["output_s3_uri"],
        "warnings": [w.as_dict() for w in warnings],
    }

    if event.get("dry_run"):
        _log(
            "dry run — nothing written",
            runId=run_id,
            compiledIntakeS3Uri=uri,
            compiledIntake=compiled,
        )
        return {**result, "status": "dry_run", "compiled_intake": compiled}

    for object_key, text in sorted(objects.items()):
        _put(s3, cfg, object_key, text, "application/sql")

    _put(s3, cfg, key, json.dumps(compiled, indent=2), "application/json")

    _log(
        "compiled intake written",
        runId=run_id,
        modelName=compiled["model_name"],
        compiledIntakeS3Uri=uri,
        sourceQueries=sorted(objects),
        populationStrategy=(compiled["population"]["strategy"] or {}).get("module"),
        labelTransformation=(compiled["label"]["transformation"] or {}).get("module"),
        warningCount=len(warnings),
    )
    return result
