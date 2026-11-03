"""Create the feature group the compiled intake describes — stage 2 (AR-05-d).

Stage 1 already decided every argument, so `CreateFeatureGroup` is issued
verbatim from the compiled intake and nothing is re-derived here.

Re-running is safe (AC-6). When the group already exists and matches the
compiled intake, creation is skipped. When it exists but differs, the difference
is reported and nothing is touched unless the caller explicitly asks for the
delete + recreate path — `OfflineStoreConfig`, both stores' CMKs, the record
identifier and the event-time column can only be set at creation time, so moving
an online-only group to online + offline means deleting it and creating it again,
which destroys the group's records.

Waiting for status `Created` is stage 3's job, not this module's.
"""

import time

import botocore.exceptions

_ALREADY_EXISTS = "ResourceInUse"
_NOT_FOUND = "ResourceNotFound"
_GLUE_NOT_FOUND = "EntityNotFoundException"

# DeleteFeatureGroup is asynchronous, so a create cannot follow it immediately.
# These bound the wait on the explicit recreate path; ordinary runs never reach it.
DELETE_MAX_WAIT_SECONDS = 300
DELETE_POLL_SECONDS = 5

# UpdateFeatureGroup --feature-additions is asynchronous too: PutRecord for a
# newly added feature can fail with a validation error if attempted before
# LastUpdateStatus settles. Stage 4 (the ingest job) waits on this before
# writing any record that carries a resolved Pattern C column.
UPDATE_MAX_WAIT_SECONDS = 300
UPDATE_POLL_SECONDS = 5

# UpdateFeatureMetadata carries no batch form -- one call per feature, unlike
# UpdateFeatureGroup's own FeatureAdditions list above. A column-derived Pattern
# C template with a real column's full cardinality (dozens to a couple hundred
# resolved features) fires that many calls back-to-back, at a rate the account's
# SageMaker API quota throttles (confirmed against real ingest data: 175 resolved
# features on one run raised ThrottlingException). Retried with the same
# exponential-backoff shape transform_and_ingest.PUT_RETRIES already uses for
# PutRecord, since it is the same kind of transient, per-call AWS throttling.
METADATA_RETRIES = 5
METADATA_BACKOFF_SECONDS = 1.0
_METADATA_TRANSIENT_ERROR_CODES = frozenset(
    {"ThrottlingException", "InternalFailure", "ServiceUnavailable"}
)


class FeatureGroupError(Exception):
    """The feature group cannot be created as the compiled intake describes it."""


def _error_code(exc):
    return exc.response.get("Error", {}).get("Code", "")


def describe(sm, name):
    """The group's description, or None when no group by that name exists."""
    try:
        return sm.describe_feature_group(FeatureGroupName=name)
    except botocore.exceptions.ClientError as exc:
        if _error_code(exc) == _NOT_FOUND:
            return None
        raise


def _definitions(feature_definitions):
    return {d["FeatureName"]: d["FeatureType"] for d in feature_definitions or []}


def _online_kms_key(document):
    return ((document.get("OnlineStoreConfig") or {}).get("SecurityConfig") or {}).get(
        "KmsKeyId"
    )


def _offline_kms_key(document):
    return (
        (document.get("OfflineStoreConfig") or {}).get("S3StorageConfig") or {}
    ).get("KmsKeyId")


def conflicts(description, request):
    """Why the existing group cannot serve this compiled intake.

    An empty list means a re-run is a safe no-op. Everything compared here is
    fixed at creation time, so a non-empty list can only be resolved by delete +
    recreate — the caller decides whether that is acceptable, not this module.

    Tags are deliberately not compared: they can be changed on a live group, and
    `ar:runid` differs on every run by design.
    """
    reasons = []

    status = description.get("FeatureGroupStatus")
    if status in ("CreateFailed", "DeleteFailed"):
        reasons.append(f"its status is {status}")

    for field in ("RecordIdentifierFeatureName", "EventTimeFeatureName"):
        if description.get(field) != request.get(field):
            reasons.append(
                f"{field} is {description.get(field)!r}, "
                f"the intake asks for {request.get(field)!r}"
            )

    if _definitions(description.get("FeatureDefinitions")) != _definitions(
        request.get("FeatureDefinitions")
    ):
        reasons.append("its feature definitions differ from the intake's")

    # Store presence, not just config: an online-only group cannot gain an
    # offline store, which is the documented recreate case (AC-6). The CMK on a
    # store (AC-3) is create-time only too, but it is only worth reporting when
    # both sides have that store — otherwise it just restates the line above.
    for label, key, kms_key_of in (
        ("online", "OnlineStoreConfig", _online_kms_key),
        ("offline", "OfflineStoreConfig", _offline_kms_key),
    ):
        present, wanted = bool(description.get(key)), bool(request.get(key))
        if present != wanted:
            reasons.append(
                f"its {label} store is not {'on' if wanted else 'off'} as the intake asks"
            )
        elif present and kms_key_of(description) != kms_key_of(request):
            reasons.append(f"its {label} store uses a different KMS key")

    return reasons


def create(sm, request):
    """Issue CreateFeatureGroup. Returns the ARN, or None if the group exists.

    None covers the narrow race where something created the group between the
    describe and this call: the group exists, which is the outcome we wanted.
    """
    try:
        return sm.create_feature_group(**request)["FeatureGroupArn"]
    except botocore.exceptions.ClientError as exc:
        if _error_code(exc) == _ALREADY_EXISTS:
            return None
        raise


def data_catalog_config(request):
    """The offline store's Glue catalog config, or None for an online-only group."""
    offline = request.get("OfflineStoreConfig") or {}
    return offline.get("DataCatalogConfig") or None


def delete_glue_table(glue, catalog):
    """Remove the Glue table DeleteFeatureGroup leaves behind. Returns its name."""
    if not catalog:
        return None
    database, table = catalog.get("Database"), catalog.get("TableName")
    if not database or not table:
        return None
    try:
        glue.delete_table(DatabaseName=database, Name=table)
    except botocore.exceptions.ClientError as exc:
        # Already gone is the desired state, not a failure.
        if _error_code(exc) != _GLUE_NOT_FOUND:
            raise
        return None
    return f"{database}.{table}"


def delete_and_wait(
    sm,
    name,
    max_wait_seconds=DELETE_MAX_WAIT_SECONDS,
    poll_seconds=DELETE_POLL_SECONDS,
):
    """Delete the group and wait until it is gone, so a create can follow.

    This destroys the group's online records and orphans its offline data. The
    caller only reaches here by asking for it.
    """
    sm.delete_feature_group(FeatureGroupName=name)
    deadline = time.monotonic() + max_wait_seconds
    while describe(sm, name) is not None:
        if time.monotonic() >= deadline:
            raise FeatureGroupError(
                f"{name} was still present {max_wait_seconds}s after "
                "DeleteFeatureGroup; re-run once the delete has completed."
            )
        time.sleep(poll_seconds)


# UpdateFeatureGroup's own real limit — confirmed against the live API
# ("Member must have length less than or equal to 100"). A column-derived
# Pattern C feature can easily resolve into more than 100 concrete names in
# one form (e.g. one dynamic name crossed over dozens of real EVENT_GROUP
# values), so a single call is not guaranteed to fit.
MAX_FEATURE_ADDITIONS_PER_CALL = 100


def _wait_for_update(sm, name, max_wait_seconds, poll_seconds):
    deadline = time.monotonic() + max_wait_seconds
    while True:
        description = sm.describe_feature_group(FeatureGroupName=name)
        status = description.get("LastUpdateStatus", {}).get("Status")
        if status == "Successful":
            return
        if status == "Failed":
            reason = description.get("LastUpdateStatus", {}).get("FailureReason", "")
            raise FeatureGroupError(
                f"UpdateFeatureGroup --feature-additions on {name} failed: {reason}"
            )
        if time.monotonic() >= deadline:
            raise FeatureGroupError(
                f"UpdateFeatureGroup --feature-additions on {name} did not reach "
                f"Successful within {max_wait_seconds}s (last status: {status!r})."
            )
        time.sleep(poll_seconds)


def add_features(
    sm,
    name,
    feature_additions,
    max_wait_seconds=UPDATE_MAX_WAIT_SECONDS,
    poll_seconds=UPDATE_POLL_SECONDS,
):
    """`UpdateFeatureGroup --feature-additions`, then wait for it to settle.

    This is the mechanism a column-derived Pattern C feature relies on: the
    group is created (stage 2) with only the statically-known features, and a
    template whose real names depend on live data is added here, by the
    ingest job, once it has resolved them from its own already-fetched source
    DataFrame — no new Snowflake connection, no state-machine reorder.

    Batched at `MAX_FEATURE_ADDITIONS_PER_CALL`, one call at a time — a
    second `UpdateFeatureGroup` on the same group cannot start until the
    first's `LastUpdateStatus` settles, so each batch waits before the next
    is issued, the same wait this function already did for a single call.

    A group with nothing to add is left untouched (`UpdateFeatureGroup`
    rejects an empty `FeatureAdditions`, and there is nothing to wait for).
    """
    for start in range(0, len(feature_additions), MAX_FEATURE_ADDITIONS_PER_CALL):
        batch = feature_additions[start : start + MAX_FEATURE_ADDITIONS_PER_CALL]
        if not batch:
            continue
        sm.update_feature_group(FeatureGroupName=name, FeatureAdditions=batch)
        _wait_for_update(sm, name, max_wait_seconds, poll_seconds)


def apply_metadata(sm, calls):
    """Publish each feature's description and lineage parameters.

    Separate from creation because UpdateFeatureMetadata only applies to a group
    that has reached `Created`, which is what stage 3 confirms. Returns the
    number of features documented.

    Each call is retried on its own with exponential backoff on a transient
    AWS-side error (chiefly ThrottlingException — there is no batch form of
    this API, so a column-derived Pattern C template with real cardinality
    fires one call per resolved feature, fast enough to hit the account's API
    rate limit). A non-transient error still fails the run immediately: it
    means this call's own arguments are wrong, and no amount of retrying
    fixes that.
    """
    for call in calls:
        for attempt in range(METADATA_RETRIES + 1):
            try:
                sm.update_feature_metadata(**call)
                break
            except botocore.exceptions.ClientError as exc:
                if _error_code(exc) not in _METADATA_TRANSIENT_ERROR_CODES:
                    raise
                if attempt == METADATA_RETRIES:
                    raise
                time.sleep(METADATA_BACKOFF_SECONDS * 2**attempt)
    return len(calls)
