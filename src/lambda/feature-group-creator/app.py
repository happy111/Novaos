"""Feature group creater — stage 2 of the feature-ingestion state machine.

Uses the compiled intake's feature list, written per src/common/intake_compiler.py.

Reads the compiled intake stage 1 wrote and issues `CreateFeatureGroup` from it.
Waiting for status `Created` is stage 3.

Event:
    {
      "run_id":                  "r5f3a9c2d1e0b",
      "compiled_intake_s3_uri":  "s3://.../compiled_intake.json",
      "action":                  "create",   # optional: create (default) | document
      "recreate":                false       # optional, see below
    }

`action: "document"` publishes the per-feature descriptions via
`UpdateFeatureMetadata`. It is a separate invocation because that call only
applies to a group that has reached `Created`, so the state machine runs it after
stage 3 rather than here.

`recreate: true` is the documented delete + recreate path (AR-05-d AC-6). It is
opt-in because it DELETES the feature group and its records. Without it, a group
that exists but does not match the compiled intake is reported, not modified.

`recreate: true` forces the delete + recreate UNCONDITIONALLY once a group
exists — not only when `conflicts()` finds an attribute mismatch. `conflicts()`
only compares what `CreateFeatureGroup` fixes at creation time (identifier,
event-time, feature definitions, store config); it cannot see e.g. a Glue table
that fell out of sync with an otherwise-matching group. An operator who asked
for `recreate` wants the delete + recreate to happen, not to be silently
skipped because the one thing they are trying to fix is invisible to this
schema-only check.
"""

import json

import boto3

from src.common import compiled_intake
from src.common import config as config_module
from src.common import feature_group, intake_schema

ACTIONS = ("create", "document")


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
    bucket, key = _split_s3_uri(uri, field)
    response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account_id)
    return response["Body"].read().decode("utf-8")


def _parse_event(event):
    run_id = str(event.get("run_id") or "")
    if not intake_schema.is_valid_run_id(run_id):
        raise EventError(
            f"run_id: {run_id!r} must be 'r' followed by 12 lowercase base32 "
            "characters, e.g. r5f3a9c2d1e0b."
        )
    if not event.get("compiled_intake_s3_uri"):
        raise EventError("compiled_intake_s3_uri: is required.")
    action = str(event.get("action") or "create")
    if action not in ACTIONS:
        raise EventError(f"action: {action!r} must be one of {', '.join(ACTIONS)}.")
    return run_id, event["compiled_intake_s3_uri"], action, bool(event.get("recreate"))


def handler(event, context):
    run_id, uri, action, recreate = _parse_event(event)
    # ENV catches a document compiled for one environment handed to another;
    # ACCOUNT_ID is the ExpectedBucketOwner on the read below.
    env_name = config_module.load_var("ENV")
    account_id = config_module.load_var("ACCOUNT_ID")
    s3 = boto3.client("s3")
    sm = boto3.client("sagemaker")

    intake = compiled_intake.parse(
        _read_s3_text(s3, uri, "compiled_intake_s3_uri", account_id)
    )
    fg_name = intake["feature_group_name"]
    request = intake["create_feature_group"]

    # The run id is minted once per execution and is part of the object key, so a
    # mismatch means this run was handed another run's document.
    if intake["run_id"] != run_id:
        raise EventError(
            f"run_id: event {run_id!r} does not match the compiled intake's "
            f"{intake['run_id']!r}."
        )
    if intake["env"] != env_name:
        raise EventError(
            f"env: the compiled intake was compiled for {intake['env']!r} but this "
            f"Lambda runs in {env_name!r}."
        )

    result = {
        "run_id": run_id,
        "feature_group_name": fg_name,
        "compiled_intake_s3_uri": uri,
        "online_enabled": bool(request.get("OnlineStoreConfig")),
        "offline_enabled": bool(request.get("OfflineStoreConfig")),
    }

    if action == "document":
        documented = feature_group.apply_metadata(sm, intake["update_feature_metadata"])
        _log(
            "feature metadata published",
            runId=run_id,
            featureGroup=fg_name,
            featuresDocumented=documented,
        )
        return {**result, "status": "documented", "features_documented": documented}

    existing = feature_group.describe(sm, fg_name)
    if existing is not None:
        reasons = feature_group.conflicts(existing, request)
        if not reasons and not recreate:
            # AC-6: a re-run of an unchanged intake creates nothing.
            _log(
                "feature group already exists — creation skipped",
                runId=run_id,
                featureGroup=fg_name,
                featureGroupStatus=existing.get("FeatureGroupStatus"),
            )
            return {
                **result,
                "status": "exists",
                "feature_group_arn": existing.get("FeatureGroupArn"),
            }
        if reasons and not recreate:
            raise feature_group.FeatureGroupError(
                f"{fg_name} exists but does not match the compiled intake: "
                + "; ".join(reasons)
                + ". Each of these is fixed at creation time, so changing it needs "
                'the delete + recreate path: re-invoke with "recreate": true, which '
                "DELETES the feature group and its records."
            )
        # recreate: true forces delete + recreate unconditionally, even with no
        # attribute conflict — the operator's own escape hatch for state that
        # conflicts() cannot see (e.g. a Glue table that fell out of sync with a
        # still-matching feature group), not only for a genuine attribute mismatch.
        _log(
            "delete + recreate requested",
            runId=run_id,
            featureGroup=fg_name,
            reasons=reasons or ["recreate explicitly requested"],
        )
        feature_group.delete_and_wait(sm, fg_name)
        dropped_table = feature_group.delete_glue_table(
            boto3.client("glue"), feature_group.data_catalog_config(request)
        )
        _log(
            "offline store Glue table dropped",
            runId=run_id,
            featureGroup=fg_name,
            glueTable=dropped_table,
        )
        status = "recreated"
    else:
        status = "created"

    arn = feature_group.create(sm, request)
    if arn is None:
        status = "exists"

    _log(
        "CreateFeatureGroup issued",
        runId=run_id,
        featureGroup=fg_name,
        status=status,
        featureGroupArn=arn,
        onlineEnabled=result["online_enabled"],
        offlineEnabled=result["offline_enabled"],
    )
    return {**result, "status": status, "feature_group_arn": arn}
