

"""Feature intake compiler — stage 1 of the feature-ingestion state machine.

Validates the intake form against src/common/intake_schema.py.

Reads a feature-intake form and its source SQL from S3, validates the form, and
writes two objects under `<FeatureGroupName>/<run id>/compile-output/`:

NOTE: this image's ECR tag is the git-log hash of THIS directory only
(cdk_pipeline/scripts/common.sh), even though the Dockerfile also COPYs
src/common/ — a src/common/-only change (e.g. glue_database()) doesn't change
the tag, so push-image.sh sees the old tag already in ECR and skips the push.

    compiled_intake.json   the contract every later stage reads
    clipped_source.sql     the source SQL wrapped in the point-in-time clip,
                           referenced by the document as ingest.source_sql_s3_uri
                           rather than inlined, because a query has no size bound

Event — the execution input, whole, plus what the state machine minted:
    {
      "model_id":               "brand_abc/patient_outcome_mortality",
      "feature_group_name":     "demo_brandabc_patient_outcome_metrics",
      "feature_intake_s3_uri":  "s3://.../feature-intake/<model>/<group>/",  # or this
      "run_id":                 "r5f3a9c2d1e0b",         # optional, see platform
      "platform": {"generated_run_id": "r5f3a9c2d1e0b"},  # minted by the ASL
      "as_of":                  "2026-09-15T00:00:00Z",  # optional, defaults to now
      "image_tag":              "novos-ar-job-...",      # optional, see compute.image_uri
      "dry_run":                false                    # optional
    }

Returns the compiled intake's S3 URI for the next stage, plus the `ingest.compute`
block and the ingest role ARN. A malformed form raises
ValidationError listing every offending field, and nothing is written.

`dry_run` compiles and returns the CreateFeatureGroup + UpdateFeatureMetadata
requests without writing anything — AR-05-a demo step 1.
"""

import json

import boto3
import yaml

from src.common import config as config_module
from src.common import intake_schema
from src.common.intake_compiler import (
    clipped_source_key,
    compile_intake,
    compiled_intake_key,
)

# The prefix a team's feature-intake directory sits under in the config bucket.
FEATURE_INTAKE_PREFIX = "feature-intake"
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


def _read_s3_text(s3, uri, field, account_id):
    """Read a caller-supplied S3 URI, refusing a bucket this account does not own."""
    bucket, key = _split_s3_uri(uri, field)
    response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account_id)
    return response["Body"].read().decode("utf-8")


def _run_id(event):
    """The caller's run id, else the one the state machine minted for this run."""
    supplied = str(event.get("run_id") or "").strip()
    if supplied:
        return supplied
    platform = event.get(PLATFORM_KEY) or {}
    return str(platform.get(GENERATED_RUN_ID_KEY) or "").strip()


def _feature_intake_uri(event, config_bucket):
    """The feature-intake directory: named outright, or composed from the model."""
    given = str(event.get("feature_intake_s3_uri") or "").strip()
    if given:
        return given
    model_id = str(event.get("model_id") or "").strip().strip("/")
    feature_group = str(event.get("feature_group_name") or "").strip().strip("/")
    if not model_id or not feature_group:
        raise EventError(
            "the execution input must carry either feature_intake_s3_uri, or both "
            "model_id and feature_group_name."
        )
    return f"s3://{config_bucket}/{FEATURE_INTAKE_PREFIX}/{model_id}/{feature_group}/"


def _parse_event(event, config_bucket):
    run_id = _run_id(event)
    if not intake_schema.is_valid_run_id(run_id):
        raise EventError(
            f"run_id: {run_id!r} must be 'r' followed by 12 lowercase base32 "
            "characters, e.g. r5f3a9c2d1e0b."
        )
    return run_id, _feature_intake_uri(event, config_bucket)


def _list_keys(s3, bucket, prefix, account_id):
    """Every object directly under `prefix`, one page being ample for a form."""
    response = s3.list_objects_v2(
        Bucket=bucket, Prefix=prefix, ExpectedBucketOwner=account_id
    )
    return [item["Key"] for item in response.get("Contents", [])]


def _one_suffix(keys, suffixes, label, uri):
    """The single key ending in one of `suffixes`, or a field-level error.

    Same rule the runbook's validate_intake.py applies on the operator's machine,
    so a directory that validates locally resolves identically here.
    """
    matches = [key for key in keys if key.lower().endswith(suffixes)]
    if not matches:
        raise EventError(
            f"feature_intake_s3_uri: no {label} ({' or '.join(suffixes)}) under {uri}."
        )
    if len(matches) > 1:
        names = ", ".join(sorted(key.rsplit("/", 1)[-1] for key in matches))
        raise EventError(
            f"feature_intake_s3_uri: expected one {label} under {uri}, "
            f"found {len(matches)}: {names}."
        )
    return matches[0]


def resolve_intake_objects(s3, feature_intake_s3_uri, account_id):
    """(form uri, sql uri) for a feature-intake DIRECTORY in S3."""
    bucket, prefix = _split_s3_uri(
        feature_intake_s3_uri.rstrip("/") + "/_", "feature_intake_s3_uri"
    )
    prefix = prefix[: -len("_")]
    keys = _list_keys(s3, bucket, prefix, account_id)
    if not keys:
        raise EventError(
            f"feature_intake_s3_uri: {feature_intake_s3_uri} is empty or does not exist."
        )

    form_key = prefix + intake_schema.FORM_FILENAME
    if form_key not in keys:
        raise EventError(
            f"feature_intake_s3_uri: no {intake_schema.FORM_FILENAME} directly under "
            f"{feature_intake_s3_uri}. The form is discovered by that exact name."
        )
    form_uri = f"s3://{bucket}/{form_key}"

    declared = ""
    parsed = yaml.safe_load(_read_s3_text(s3, form_uri, "intake form", account_id))
    if isinstance(parsed, dict):
        # source.source_query is a {path, params} block in the canon template,
        # not a bare string — normalize before treating it as a path.
        declared = intake_schema.query_block(
            (parsed.get("source") or {}).get("source_query")
        )["path"]
    if declared:
        wanted = declared.rsplit("/", 1)[-1]
        for key in keys:
            if key.rsplit("/", 1)[-1] == wanted:
                return form_uri, f"s3://{bucket}/{key}"

    sql_key = _one_suffix(keys, (".sql",), "source SQL", feature_intake_s3_uri)
    return form_uri, f"s3://{bucket}/{sql_key}"


def handler(event, context):
    cfg = config_module.load()
    run_id, feature_intake_s3_uri = _parse_event(event, cfg.config_bucket)
    s3 = boto3.client("s3")

    form_uri, sql_uri = resolve_intake_objects(
        s3, feature_intake_s3_uri, cfg.account_id
    )
    _log(
        "compiling feature intake",
        runId=run_id,
        featureIntakeDir=feature_intake_s3_uri,
        intakeForm=form_uri,
        sourceSql=sql_uri,
    )

    form = yaml.safe_load(_read_s3_text(s3, form_uri, "intake form", cfg.account_id))
    source_sql = _read_s3_text(s3, sql_uri, "source SQL", cfg.account_id)

    # Field-level validation before anything is created (AR-05-a AC-2). No
    # intake_dir: this Lambda fetches the form and SQL as S3 text, never a
    # local directory, so a feature's transformation is checked structurally
    # (module/function/source_columns present and well-shaped) but not against
    # its real signature — that full check is the launcher-side
    # docs/runbooks/feature-ingestion/validate_intake.py's job, which has a
    # staged directory to check against.
    intake_schema.validate_or_raise(form)

    compiled, clipped_sql = compile_intake(
        cfg,
        form,
        source_sql,
        run_id,
        as_of=event.get("as_of"),
        image_tag=str(event.get("image_tag") or "").strip() or None,
    )
    fg_name = compiled["feature_group_name"]
    key = compiled_intake_key(fg_name, run_id)
    uri = f"s3://{cfg.compiled_intake_bucket}/{key}"
    sql_key = clipped_source_key(fg_name, run_id)
    sql_uri = compiled["ingest"]["source_sql_s3_uri"]

    if event.get("dry_run"):
        _log(
            "dry run — nothing written",
            runId=run_id,
            featureGroup=fg_name,
            compiledIntakeS3Uri=uri,
            clippedSourceS3Uri=sql_uri,
            createFeatureGroup=compiled["create_feature_group"],
            updateFeatureMetadata=compiled["update_feature_metadata"],
        )
        return {
            "status": "dry_run",
            "run_id": run_id,
            "feature_group_name": fg_name,
            "compiled_intake_s3_uri": uri,
            "compiled_intake": compiled,
        }

    s3.put_object(
        Bucket=cfg.compiled_intake_bucket,
        Key=sql_key,
        Body=clipped_sql.encode("utf-8"),
        ContentType="application/sql",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=cfg.kms_key_arn,
        ExpectedBucketOwner=cfg.account_id,
    )

    s3.put_object(
        Bucket=cfg.compiled_intake_bucket,
        Key=key,
        Body=json.dumps(compiled, indent=2).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=cfg.kms_key_arn,
        ExpectedBucketOwner=cfg.account_id,
    )

    _log(
        "compiled intake written",
        runId=run_id,
        featureGroup=fg_name,
        compiledIntakeS3Uri=uri,
        clippedSourceS3Uri=sql_uri,
        featureCount=len(compiled["ingest"]["transforms"]),
    )

    return {
        "status": "ok",
        "run_id": run_id,
        "feature_group_name": fg_name,
        "compiled_intake_s3_uri": uri,
        "feature_count": len(compiled["ingest"]["transforms"]),
        "online_ttl_mode": compiled["online_ttl"]["mode"],
        "recreate": bool(event.get("recreate")),
        # Needed by stage 4
        "ingest_compute": compiled["ingest"]["compute"],
        "ingest_role_arn": compiled["ingest"]["role_arn"],
    }
