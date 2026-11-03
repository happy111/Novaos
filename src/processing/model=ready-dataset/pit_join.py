"""Point-in-time join + leakage check — stage 2 of the model-ready-dataset machine.

The first SageMaker Processing job of AR-18-b-2. Runs as `CohortLabelExecution`, reads
the labelled cohort per `source.source_location`, attaches each feature group's offline
store as of every row's own cutoff, and writes the joined cohort for the split stage.

    in:  --compiled-intake-s3-uri   the contract stage 1 wrote
         --run-id                   the run id the state machine minted
    out: cohort_with_features.parquet + pit_join_report.json under
         jobs.pit_join.output_s3_uri

The YAML intake form is never re-parsed: the compiled intake is the contract between
the stages and nothing here re-derives what stage 1 decided (SPEC-M4 §2.3).

TWO INDEPENDENT REJECTIONS, TWO INDEPENDENT REPORT ENTRIES (AC-2, AC-3). They are
separate problems and a feature can pass one and fail the other:

  1. TARGET-DERIVED — a column named in `point_in_time.disallowed_feature_names` is
     dropped from the offline read BEFORE the join. A feature computed from the
     outcome is leaky no matter when it arrived, so no timestamp makes it acceptable.
  2. TEMPORAL — a feature row whose `event_time` is after the row's
     `point_in_time.feature_cutoff` value is excluded from the as-of pick. A perfectly
     legitimate feature is leaky for the rows it arrived too late for.

Neither predicate comes from `ar_transformations`: the distribution has no leakage
tier (seven tiers as of `0af80e7`: features, labels, population, preprocessing,
sampling, splits, validation) and the MRD template carries no dispatch block through
which a job could name one. Both are therefore applied by the platform here, the same
status the three `validation` thresholds have. The absent tier is an AR-18-b-1 gap,
reported rather than worked around — see the handoff.

`feature_cutoff` names EITHER a column the cohort already carries (the common case —
`observation_window_end` in the cosentyx form, the MASKING-AWARE cutoff, not
`anchor_date` and not `premask_observation_window_end`: masking puts them months
apart, and using either of those would let masked rows read past their own cutoff) OR
a literal date shared by every row (`resolve_cutoff_column`) — a hard cutoff the whole
cohort shares, a real, distinct use case rather than a misconfiguration. A per-row
column is read, never recomputed: two implementations of one windowing rule are free
to drift (SPEC-M4 §2.3).

Nothing is read through a Processing channel. Every object is fetched with boto3 so
that `ExpectedBucketOwner` is on every call, which a `ProcessingInput` download cannot
be — the same reason the cohort-label job needs no channels. It is also the only
mechanism that covers all three `source_location` modes: a Snowflake query and an
Athena read of the offline store are not mountable as channels at all.

No Snowflake connection detail appears in the compiled intake. It arrives as
environment variables carrying secret ARNs, never secret values (SPEC-M4 §6.5).
"""

import argparse
import io
import json
import os
import sys
import time
import uuid

import boto3
import pandas as pd

from src.common import intake_schema
from src.common.mrd_compiler import utc_now
from src.common.mrd_schema import ALLOWED_JOIN_TYPES, DEFAULT_JOIN_TYPE

# config.py and engine_dispatch.py sit beside this script. The stage directory is
# hyphenated and so is not an importable package (SPEC-M4 §6.6), which is why they
# are reached by path rather than by `from src.processing...`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as job_config  # noqa: E402 — needs the path insert above
import engine_dispatch  # noqa: E402 — same
import s3_io  # noqa: E402 — same

PARQUET_OBJECT = "cohort_with_features.parquet"
# A fixed name, not timestamped: the output prefix is [Run ID]/pit-join/, already
# unique per run, so a timestamp would only make the object harder to find.
REPORT_OBJECT = "pit_join_report.json"

# Top-level keys this job reads. A missing one means the document was written by an
# incompatible stage 1, not something to recompute from the form.
REQUIRED_KEYS = (
    "run_id",
    "env",
    "model_name",
    "upstream",
    "source",
    "dataset_assembly",
    "point_in_time",
    "jobs",
)

# Feature Store offline housekeeping columns — not features, dropped before the join.
HOUSEKEEPING = ("write_time", "api_invocation_time", "is_deleted")
# Named because the server-side as-of pick ORDERS BY it. Not projected: a window's
# ORDER BY reads any column of the table, so the tie-break costs nothing in the result.
WRITE_TIME_COLUMN = "write_time"

# Feature Store has exactly three feature types. Parquet carries its own, but a nullable
# Integral column arrives as a float, so the declared type is applied either way.
FS_TYPE_TO_PANDAS = {"Fractional": "float64", "Integral": "Int64", "String": "string"}

# Feature types whose values can be written as a SQL literal the stored column will
# compare equal to. A Fractional key is excluded: SQL float equality is not pandas'.
PUSHDOWN_TYPES = ("String", "Integral")

# The one documented shape of `dataset_assembly.dedup_rule`, which the template gives
# as free text ("keep latest by anchor_date") and stage 1 does not parse. Anything
# else is refused rather than silently ignored — a dedup rule that does not run is a
# cohort with duplicate rows the author believed were gone.
DEDUP_PREFIX = "keep latest by "

# Working columns the join adds and drops again. Prefixed so they cannot collide with
# a cohort or feature column, since either side's names come from a form.
KEY_COLUMN_PREFIX = "_pit_key_"
CUTOFF_COLUMN = "_pit_cutoff"
EVENT_TIME_COLUMN = "_pit_event_time"
MATCHED_COLUMN = "_pit_matched"
# feature_cutoff resolves to this synthetic column when the form names a literal
# date rather than a per-row column — see resolve_cutoff_column.
LITERAL_CUTOFF_COLUMN = "_pit_literal_cutoff"
# The window column of the SERVER-SIDE as-of pick (`offline_store_sql`). Dropped after
# the read: it is machinery, and a column that reached the dataset would be read as a
# feature. Prefixed like the others so a form's own column cannot collide with it.
AS_OF_RANK_COLUMN = "_pit_rank"

# A login that hangs would otherwise burn the job's whole runtime.
SNOWFLAKE_TIMEOUT_SECONDS = 30
# A single query's own ceiling — distinct from the login timeout above. The
# real cohort read over the FULL (uncapped) population can legitimately run
# for a long time; capping it at the same 30s as a login handshake would
# cancel it client-side well before Snowflake itself would have (confirmed
# against real AWS on the sibling cohort-label pipeline's own identical bug).
# Set to this Processing job's own MaxRuntimeInSeconds
# (environments/<env>.json's state_machines.model_ready_dataset.pit_join
# .max_runtime_seconds) — a query timeout longer than the job's own ceiling
# would never fire; SageMaker would kill the job first.
SNOWFLAKE_QUERY_TIMEOUT_SECONDS = 86400
# The connection secret is a JSON document; this is the field holding the login.
SNOWFLAKE_USER_FIELD = "snow_user"
SNOWFLAKE_USER_DOMAIN = "@NOVARTIS.NET"

# Athena poll interval. The offline store is Parquet on S3, so a cohort-sized scan
# finishes in seconds; a shorter interval would only add API calls.
ATHENA_POLL_SECONDS = 2
# Athena refuses a DML statement over 262,144 bytes. This is the budget for the SELECT
# alone — the rest is the UNLOAD wrapper, a few hundred bytes of destination URI. A
# cohort large enough to carry the key pushdown past it loses the pushdown rather than
# the query (`offline_store_sql`).
ATHENA_MAX_QUERY_BYTES = 260000


class PitJoinError(Exception):
    """The job cannot run: a malformed argument, an unusable compiled intake, or
    source data that does not match what the compiled intake declared."""


# The S3 helpers and the structured logger live in `s3_io.py`, shared with the other
# two stages of this pipeline.
_log = s3_io.log


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Point-in-time join + leakage check (stage 2 of the "
        "model-ready-dataset pipeline)."
    )
    parser.add_argument("--compiled-intake-s3-uri", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    # Reuses the delivered helper so both pipelines agree on the format rather than
    # carrying a second definition of it.
    if not intake_schema.is_valid_run_id(args.run_id):
        raise PitJoinError(
            f"run_id: {args.run_id!r} must be 'r' followed by 12 lowercase base32 "
            "characters, e.g. r5f3a9c2d1e0b."
        )
    return args


def parse_intake(text):
    """Load the compiled intake and check the keys this job reads."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise PitJoinError(f"compiled intake is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PitJoinError("compiled intake must be a JSON object.")
    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        raise PitJoinError("compiled intake is missing key(s): " + ", ".join(missing))
    return document


# --- reading the cohort ------------------------------------------------------


def read_s3_cohort(s3, s3_uri, account_id, run_id):
    """Read every parquet object under the resolved source prefix.

    `source.s3_uri` is a versioned directory, not a single object: stage 1 already did
    the listing that resolves "latest version", so the version is fixed by the time
    this runs and only the objects inside it have to be gathered.
    """
    bucket, prefix = s3_io.split_uri(s3_uri, "source.s3_uri")
    prefix = prefix.rstrip("/") + "/"
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix=prefix, ExpectedBucketOwner=account_id
    ):
        keys += [
            item["Key"]
            for item in page.get("Contents", [])
            if item["Key"].endswith(".parquet")
        ]
    if not keys:
        raise PitJoinError(
            f"source.s3_uri: no .parquet object under {s3_uri}. The upstream "
            f"cohort-label run writes population_labeled.parquet there."
        )

    frames = []
    for key in sorted(keys):
        body = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account_id)[
            "Body"
        ].read()
        frames.append(pd.read_parquet(io.BytesIO(body)))
    _log("cohort read from S3", runId=run_id, sourceS3Uri=s3_uri, objects=len(keys))
    return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]


def connect_to_snowflake(secrets, settings):
    """Key-pair auth with the PEM fetched from Secrets Manager (SPEC-M4 §6.5).

    The connector and cryptography are imported here rather than at module scope so
    the unit tests, which never connect, do not need either installed.

    `src/processing/cohort-label/population_label.py` carries the same function. The
    duplication is deliberate: deduplication across the two pipelines is deferred, and
    the stage directories are hyphenated so neither can import the other.
    """
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization

    connection_secret = secrets.get_secret_value(
        SecretId=settings["SNOWFLAKE_USER_SECRET_ID"]
    )["SecretString"]
    user = json.loads(connection_secret)[SNOWFLAKE_USER_FIELD] + SNOWFLAKE_USER_DOMAIN
    # The stored PEM is wrapped in triple quotes, which load_pem_private_key rejects.
    pem = secrets.get_secret_value(
        SecretId=settings["SNOWFLAKE_PRIVATE_KEY_SECRET_ID"]
    )["SecretString"].replace('"""', "")
    private_key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)

    return snowflake.connector.connect(
        account=settings["SNOWFLAKE_ACCOUNT"],
        host=settings["SNOWFLAKE_HOST"],
        user=user,
        private_key=private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        warehouse=settings["SNOWFLAKE_WAREHOUSE"],
        database=settings["SNOWFLAKE_DATABASE"],
        schema=settings["SNOWFLAKE_SCHEMA"],
        role=settings["SNOWFLAKE_ROLE"],
        login_timeout=SNOWFLAKE_TIMEOUT_SECONDS,
        network_timeout=SNOWFLAKE_QUERY_TIMEOUT_SECONDS,
    )


def read_snowflake_cohort(s3, secrets, config, source, run_id):
    """Run the rendered cohort query against Snowflake over PrivateLink.

    The SQL is by reference, not inline: stage 1 wrote it to
    `source.source_sql_s3_uri` with every `${...}` already substituted, so nothing
    here needs the form's `params`.
    """
    sql = s3_io.read_text(
        s3, source["source_sql_s3_uri"], "source.source_sql_s3_uri", config.account_id
    )
    settings = config.require_snowflake()
    _log(
        "reading the cohort from Snowflake over PrivateLink",
        runId=run_id,
        sourceSql=source["source_sql_s3_uri"],
        snowflakeAccount=settings["SNOWFLAKE_ACCOUNT"],
    )
    connection = connect_to_snowflake(secrets, settings)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
            frame = cursor.fetch_pandas_all()
        finally:
            cursor.close()
    finally:
        connection.close()
    # Snowflake returns unquoted identifiers upper-cased, while every column a form
    # names is written lower-case, so the fetch is normalised once here.
    frame.columns = [str(name).lower() for name in frame.columns]
    return frame


# --- reading the offline store ------------------------------------------------


def offline_table(sagemaker, feature_group_name):
    """The group's offline Glue database/table, event-time column and feature types.

    All four come from the M2 control plane, never hardcoded. V12: because
    `DisableGlueTableCreation` is False the table exists under the database M2 named
    explicitly, which `DataCatalogConfig` is the only reliable source of.
    """
    described = sagemaker.describe_feature_group(FeatureGroupName=feature_group_name)
    offline = described.get("OfflineStoreConfig") or {}
    catalog = offline.get("DataCatalogConfig") or {}
    if not catalog.get("Database") or not catalog.get("TableName"):
        raise PitJoinError(
            f"feature group {feature_group_name!r} has no offline store data catalog, "
            f"so there is nothing to join point-in-time. The PIT join reads the "
            f"OFFLINE store (AC-1)."
        )
    types = {
        definition["FeatureName"]: definition["FeatureType"]
        for definition in described.get("FeatureDefinitions", [])
    }
    return (
        catalog["Database"],
        catalog["TableName"],
        described["EventTimeFeatureName"],
        types,
    )


def unload_to_parquet(athena, s3, sql, config, run_id, label):
    """Run one Athena query as an UNLOAD and read its Parquet output into a frame.

    UNLOAD rather than `get_query_results`: that API returns at most 1000 rows a page
    and every value as a string, so a 42M-row group is 42,000 API calls and a whole
    offline store re-typed from text. Athena has to write the result set to S3 either
    way, so it is written as Parquet and read back — one listing and one GET per part,
    with the types the table declares.

    The destination is unique per call. UNLOAD refuses a location that already holds
    objects, which a retried run under the same run id would hand it.

    The Parquet parts are written SSE-KMS EXPLICITLY rather than left to the bucket's
    default. Athena sends no `x-amz-server-side-encryption` header unless asked, and the
    dataset bucket denies a put without one — which is how this read failed outright once
    that bucket was recreated with such a policy. `EncryptionConfiguration` covers the
    UNLOAD destination as well as the result set, not only the latter: verified against
    the real service, the part files come back `aws:kms` under this CMK.

    Returns None where the query matched nothing: UNLOAD writes no object at all for an
    empty result, and only the caller knows which columns should have come back.
    """
    destination = (
        f"{config.athena_output_s3_uri.rstrip('/')}/unload/{run_id}/{uuid.uuid4().hex}/"
    )
    query_id = athena.start_query_execution(
        QueryString=f"UNLOAD ({sql}) TO '{destination}' WITH (format = 'PARQUET')",
        ResultConfiguration={
            "OutputLocation": config.athena_output_s3_uri,
            "EncryptionConfiguration": {
                "EncryptionOption": "SSE_KMS",
                "KmsKey": config.kms_key_arn,
            },
        },
    )["QueryExecutionId"]

    while True:
        status = athena.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]["Status"]
        state = status["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(ATHENA_POLL_SECONDS)
    if state != "SUCCEEDED":
        raise PitJoinError(
            f"the offline-store query {state}: "
            f"{status.get('StateChangeReason', '')}. V13: this read runs with the "
            f"job's own credentials and needs offline-store access, Glue read, an "
            f"Athena results location and kms:Decrypt."
        )

    bucket, prefix = s3_io.split_uri(destination, "ATHENA_OUTPUT_S3_URI")
    frames = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix=prefix, ExpectedBucketOwner=config.account_id
    ):
        for item in page.get("Contents", []):
            # Every part is read, not the first: UNLOAD writes one object per Athena
            # worker, so taking one would join nothing for the entities in the others.
            # Taken by prefix rather than by `.parquet` suffix — UNLOAD names its parts
            # without an extension.
            if item["Key"].endswith("/") or not item.get("Size", 1):
                continue
            body = s3.get_object(
                Bucket=bucket, Key=item["Key"], ExpectedBucketOwner=config.account_id
            )["Body"].read()
            frames.append(pd.read_parquet(io.BytesIO(body)))
    _log(
        "offline store unloaded",
        runId=run_id,
        featureGroup=label,
        unloadS3Uri=destination,
        parts=len(frames),
        rows=sum(len(frame) for frame in frames),
    )
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]


def key_literals(values, feature_type):
    """`(literals, keep_nulls)` for one join key's cohort-side values, or None.

    None where the key cannot be bounded safely rather than an approximation: a
    pushdown that is not a superset of what the join matches silently loses rows.

    `keep_nulls` because pandas' merge matches null to null, so a cohort key carrying
    nulls has to keep the store's null rows in the read or the bound would drop rows
    the join attaches.
    """
    if feature_type not in PUSHDOWN_TYPES:
        return None
    series = pd.Series(values)
    unique = series.dropna().unique()
    if not len(unique):
        return None
    try:
        if feature_type == "Integral":
            literals = [str(int(value)) for value in unique]
        else:
            literals = ["'" + str(value).replace("'", "''") + "'" for value in unique]
    except (TypeError, ValueError):
        # The cohort holds something that is not the type the group stores. The join
        # will match nothing on it, which the coverage counts report; a SQL literal
        # guessed from it would not be that honest.
        return None
    return literals, bool(series.isna().any())


def in_predicate(column, literals, keep_nulls):
    """`"column" IN (...)`, widened to admit NULL when the cohort side carries one."""
    predicate = f'"{column}" IN ({", ".join(literals)})'
    if keep_nulls:
        return f'({predicate} OR "{column}" IS NULL)'
    return predicate


def key_in_list(column, values, feature_type):
    """`"column" IN (...)` over one join key's cohort-side values, or None.

    The whole key set in one predicate. `offline_store_sql` splits it when that does
    not fit a single statement; this is the unsplit form.
    """
    resolved = key_literals(values, feature_type)
    if resolved is None:
        return None
    return in_predicate(column, *resolved)


def chunk_literals(literals, budget):
    """Split `literals` into groups whose rendered `IN` list fits `budget` bytes.

    Empty where even one literal does not fit, which the caller reads as "this key
    cannot be bounded at all". Each literal costs its own length plus the two bytes
    of the ", " that joins it to the previous one — counted for every literal rather
    than every gap, so the estimate is one separator long, never short.
    """
    groups, current, used = [], [], 0
    for literal in literals:
        cost = len(literal.encode("utf-8")) + 2
        if current and used + cost > budget:
            groups.append(current)
            current, used = [], 0
        if cost > budget:
            return []
        current.append(literal)
        used += cost
    if current:
        groups.append(current)
    return groups


def offline_store_sql(
    database,
    table,
    projection,
    event_time,
    types,
    key_values,
    max_event_time,
    as_of_keys=None,
):
    """The offline-store SELECT, with the cohort's own bounds pushed into it.

    Unbounded, this read drags every row of a 122M-row group back over Athena for a
    383,000-row cohort and discards almost all of them in pandas. Two bounds are pushed
    into the query instead, both SUPERSETS of what the join then keeps, so neither can
    change the result — they only stop rows the join would discard being transferred:

      - THE JOIN KEYS, `column IN (the cohort's distinct values)`. Passed per key rather
        than as a tuple, which over-selects for a multi-key group and so is still a
        superset. `key_values` carries only the keys NEITHER side transforms, because a
        transformed key is compared as its transformed value while the stored column
        holds something else (`join_key_values`).
      - THE EVENT TIME, `<= max(feature_cutoff)`. Every row's own cutoff is at or below
        the cohort's maximum, so a feature row past the maximum is past every row's
        cutoff. The per-row predicate is still the thing that rejects a row inside this
        bound but after its own row's cutoff.

    `try(from_iso8601_timestamp(...))` rather than a lexical string comparison: an
    event time carrying a UTC offset sorts after an earlier instant written as `Z`, and
    `try` leaves an unparseable one NULL — excluded, exactly as pandas' `errors="coerce"`
    excludes it. Only for a String event time; a Fractional one is epoch seconds.

    A KEY SET TOO LARGE FOR ONE STATEMENT IS SPLIT ACROSS SEVERAL, not dropped. Athena
    refuses a DML statement over 262,144 bytes, and a cohort reaches that sooner than it
    looks: 16,656 distinct 14-character ids render as a 300 KB `IN` list on their own. A
    dropped bound reads the whole group — 122M rows for the largest group in the cosentyx
    form — so the widest key is chunked instead and one statement issued per chunk. The
    chunks partition that key's distinct values, so their UNION selects exactly the rows
    the unsplit predicate would have, and no row can appear in two of them.

    `as_of_keys`, when given, moves THE AS-OF PICK ITSELF into the query: a window
    function returns one row per key — the latest at or before the bound — instead of
    every candidate row for pandas to sort and de-duplicate. For a 40M-row group joined
    to a 1M-row cohort that is the difference between transferring the whole bounded set
    and transferring the ~1M rows the join actually keeps.

    It is passed ONLY when the cohort's cutoff is uniform (blank, or one literal date
    shared by every row). With a per-row cutoff the pick is not a property of the key
    alone: the latest row before the cohort's MAXIMUM cutoff can be after some
    individual row's own cutoff, and the correct answer for that row is an earlier one
    this query would already have discarded. The caller decides; see `main`.

    Chunking stays correct under it, because the chunks partition the widest key's
    values — so every row sharing a key tuple lands in the SAME statement, and a rank
    computed within one statement is the rank over the whole key.

    Returns `(statements, pushdown)`. `statements` is a list; `pushdown` is what the
    report records about the bound, including how many statements it took.
    """
    pushdown = {
        "key_columns": [],
        "max_event_time": None,
        "statements": 1,
        "as_of_in_sql": bool(as_of_keys),
    }

    resolved = {}
    for column, values in (key_values or {}).items():
        literals = key_literals(values, types.get(column))
        if literals:
            resolved[column] = literals

    time_predicates = []
    if max_event_time is not None and types.get(event_time) == "String":
        bound = max_event_time.isoformat()
        time_predicates.append(
            f'try(from_iso8601_timestamp("{event_time}")) '
            f"<= from_iso8601_timestamp('{bound}')"
        )
        pushdown["max_event_time"] = bound

    # is_deleted need not be selected to be filtered on, so a form that names an
    # explicit column list still gets the deleted rows excluded.
    #
    # `as_of_keys` turns this into the AS-OF PICK rather than a plain bounded read: one
    # row per key, the latest at or before the bound, chosen by a window function
    # instead of by dragging every candidate row back for pandas to sort. Newest event
    # time wins, `write_time` breaking a tie between two ingests of the same
    # (key, event_time) — the offline store is append-only, so that pair is not unique.
    # NULLS LAST so a key with both parseable and unparseable event times picks a
    # parseable row, matching what pandas' `errors="coerce"` then keeps.
    def statement(predicates):
        clauses = " AND ".join(["is_deleted = false"] + predicates)
        inner = f'SELECT {projection} FROM "{database}"."{table}" WHERE {clauses}'
        if not as_of_keys:
            return inner
        partition = ", ".join(f'"{column}"' for column in as_of_keys)
        recency = (
            f'try(from_iso8601_timestamp("{event_time}")) DESC NULLS LAST'
            if types.get(event_time) == "String"
            else f'"{event_time}" DESC NULLS LAST'
        )
        ranked = (
            f"SELECT {projection}, ROW_NUMBER() OVER (PARTITION BY {partition} "
            f'ORDER BY {recency}, "{WRITE_TIME_COLUMN}" DESC NULLS LAST) '
            f'AS "{AS_OF_RANK_COLUMN}" FROM "{database}"."{table}" WHERE {clauses}'
        )
        return f'SELECT {projection} FROM ({ranked}) WHERE "{AS_OF_RANK_COLUMN}" = 1'

    def fits(sql):
        return len(sql.encode("utf-8")) <= ATHENA_MAX_QUERY_BYTES

    everything = [in_predicate(column, *resolved[column]) for column in resolved]
    sql = statement(everything + time_predicates)
    if fits(sql):
        pushdown["key_columns"] = list(resolved)
        return [sql], pushdown

    # Split the key whose rendered list is longest; every other predicate rides in
    # every chunk, so each statement is still a complete bound on its own.
    widest = max(
        resolved,
        key=lambda column: sum(len(lit) + 2 for lit in resolved[column][0]),
    )
    others = [
        in_predicate(column, *resolved[column])
        for column in resolved
        if column != widest
    ]
    literals, keep_nulls = resolved[widest]
    # Measured against the statement carrying an EMPTY list for the widest key, so the
    # budget is what is left for its literals once everything else is in place.
    fixed = len(
        statement(
            others + [in_predicate(widest, [], keep_nulls)] + time_predicates
        ).encode("utf-8")
    )
    groups = chunk_literals(literals, ATHENA_MAX_QUERY_BYTES - fixed)

    if not groups:
        # Not even one value of the widest key fits beside the other predicates. Keep
        # the bounds that do fit rather than failing the run; the join still filters in
        # pandas, at the cost of transferring rows it will discard.
        narrowed = statement(others + time_predicates)
        if fits(narrowed):
            pushdown["key_columns"] = [c for c in resolved if c != widest]
            return [narrowed], pushdown
        return [statement(time_predicates)], pushdown

    pushdown["key_columns"] = list(resolved)
    pushdown["statements"] = len(groups)
    return [
        statement(others + [in_predicate(widest, group, keep_nulls)] + time_predicates)
        for group in groups
    ], pushdown


def _offline_read_projection(columns, join_columns, event_time, types, as_of_keys):
    """The resolved column list and its rendered projection for the offline read.

    `columns` blank means every column, which only the control plane knows, so the
    projection falls back to `*` in that case — EXCEPT under `as_of_keys`, where the
    schema's own column list is named instead. The as-of pick wraps the read in a
    window, and `SELECT *` over that wrapper would carry the window column out with the
    features; Trino has no `* EXCEPT`, so the way to leave it behind is to name what is
    wanted. `types` is that list, which is why it comes from the control plane.

    `join_columns` and the event-time column are ADDED to a declared projection rather
    than assumed to be in it: `feature_columns` names the FEATURES a form wants, and the
    template's own example omits the key and the timestamp — without them the join has
    nothing to match on and no as-of value to compare.
    """
    selected = [column for column in columns if column not in HOUSEKEEPING]
    if not selected and as_of_keys:
        selected = [column for column in types if column not in HOUSEKEEPING]
    if selected:
        for required in list(join_columns) + [event_time]:
            if required not in selected:
                selected.append(required)
    projection = ", ".join(f'"{column}"' for column in selected) if selected else "*"
    return selected, projection


def _run_offline_statements(athena, s3, statements, config, run_id, group_name):
    """The concatenated Parquet result of every statement in `statements`, or None
    when all of them returned nothing.

    More than one statement is only ever present where the key bound had to be
    split (`offline_store_sql`). They partition the key's values, so concatenating
    is a plain append — no row can arrive twice and nothing needs de-duplicating.
    """
    parts = []
    for position, sql in enumerate(statements):
        label = group_name if len(statements) == 1 else f"{group_name}[{position}]"
        part = unload_to_parquet(athena, s3, sql, config, run_id, label)
        if part is not None:
            parts.append(part)
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]


def _drop_housekeeping_columns(frame):
    """`frame` with its write-tracking and as-of-rank columns removed, plus the
    list of what was dropped.

    The window column is named in the projection's place, never in it, so it
    should not be here — dropped anyway rather than trusted, because if it ever
    did arrive the join would attach it to the dataset as though it were a
    feature.
    """
    dropped = [
        column
        for column in frame.columns
        if column in HOUSEKEEPING or column == AS_OF_RANK_COLUMN
    ]
    return frame.drop(columns=dropped), dropped


def read_offline_features(
    athena,
    s3,
    config,
    group_name,
    columns,
    join_columns,
    run_id,
    key_values=None,
    max_event_time=None,
    as_of_keys=None,
):
    """The offline store rows for one feature group, typed from its M2 schema.

    `columns` blank means every column, which only the control plane knows, so the
    projection falls back to `*` in that case — EXCEPT under `as_of_keys`, where the
    schema's own column list is named instead. The as-of pick wraps the read in a
    window, and `SELECT *` over that wrapper would carry the window column out with the
    features; Trino has no `* EXCEPT`, so the way to leave it behind is to name what is
    wanted. `types` is that list, which is why it comes from the control plane.

    `join_columns` and the event-time column are ADDED to a declared projection rather
    than assumed to be in it: `feature_columns` names the FEATURES a form wants, and the
    template's own example omits the key and the timestamp — without them the join has
    nothing to match on and no as-of value to compare.

    `key_values` and `max_event_time` are the cohort's half of the join, bounding the
    read — see `offline_store_sql` for why neither can change what the join produces.

    Returns `(frame, event_time, pushdown)`.
    """
    sagemaker = boto3.client("sagemaker", region_name=config.region)
    database, table, event_time, types = offline_table(sagemaker, group_name)

    selected, projection = _offline_read_projection(
        columns, join_columns, event_time, types, as_of_keys
    )
    statements, pushdown = offline_store_sql(
        database,
        table,
        projection,
        event_time,
        types,
        key_values,
        max_event_time,
        as_of_keys=as_of_keys,
    )
    frame = _run_offline_statements(athena, s3, statements, config, run_id, group_name)
    # An empty result carries no Parquet to take columns from, so they come from the
    # projection — the join needs its key column present to report zero coverage rather
    # than fail on a frame that has nothing in it at all.
    if frame is None:
        frame = pd.DataFrame(columns=selected or list(types))

    frame, dropped = _drop_housekeeping_columns(frame)
    frame = cast_to_schema(frame, types)
    _log(
        "offline store read",
        runId=run_id,
        featureGroup=group_name,
        glueTable=f"{database}.{table}",
        eventTimeColumn=event_time,
        rows=len(frame),
        housekeepingDropped=dropped,
        pushdown=pushdown,
    )
    return frame, event_time, pushdown


def cast_to_schema(frame, types):
    """Apply the M2 feature types to what Parquet carried back."""
    for column in frame.columns:
        pandas_dtype = FS_TYPE_TO_PANDAS.get(types.get(column))
        if pandas_dtype and pandas_dtype != "string":
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(
                pandas_dtype
            )
    return frame


# --- cohort assembly ---------------------------------------------------------


def apply_dedup(frame, dedup_rule, entity_id, run_id):
    """Apply `dataset_assembly.dedup_rule`, before the join rather than after it.

    Deduplicating first means the join never does work for rows that are about to be
    discarded. The rule is free text in the template with no grammar defined anywhere,
    so only its one documented shape is accepted and anything else is refused: a rule
    the author wrote and the pipeline ignored is worse than one it rejects.
    """
    rule = str(dedup_rule or "").strip()
    if not rule:
        return frame
    if not rule.lower().startswith(DEDUP_PREFIX):
        raise PitJoinError(
            f"dataset_assembly.dedup_rule is {rule!r}, which this stage cannot apply. "
            f"The one supported form is {DEDUP_PREFIX!r}<column>, e.g. "
            f"'keep latest by anchor_date'."
        )
    column = rule[len(DEDUP_PREFIX) :].strip()
    if column not in frame.columns:
        raise PitJoinError(
            f"dataset_assembly.dedup_rule names column {column!r}, which the cohort "
            f"does not carry. It has {sorted(frame.columns)}."
        )
    before = len(frame)
    frame = frame.sort_values(column).drop_duplicates(subset=[entity_id], keep="last")
    _log(
        "dedup applied",
        runId=run_id,
        rule=rule,
        byColumn=column,
        entityIdColumn=entity_id,
        rowsIn=before,
        rowsOut=len(frame),
    )
    return frame.reset_index(drop=True)


def apply_cohort_cap(frame, max_cohort_size, run_id):
    """Apply `dataset_assembly.max_cohort_size`, a cap on total rows.

    Takes the first N after dedup rather than sampling: choosing rows is
    `dataset_assembly.sampling`'s job, dispatched in the split stage, and a cap that
    quietly resampled would make the two indistinguishable in the output.
    """
    cap = max_cohort_size
    if not cap:
        return frame
    cap = int(cap)
    if len(frame) <= cap:
        return frame
    _log(
        "cohort cap applied",
        runId=run_id,
        maxCohortSize=cap,
        rowsIn=len(frame),
        rowsOut=cap,
    )
    return frame.head(cap).reset_index(drop=True)


def resolve_cutoff_column(cohort, feature_cutoff, run_id):
    """`point_in_time.feature_cutoff` names EITHER a per-row column the cohort
    already carries, OR a literal date shared by every row — resolve to the
    real column name the rest of this job reads.

    A per-row column is the common case (the cosentyx form's
    `observation_window_end`, the masking-aware cutoff): each entity's own
    safe cutoff, since masking can put it months away from a shared anchor. A
    literal date is a real, distinct use case (confirmed by the team, not a
    misconfiguration): a hard cutoff the whole cohort shares, beyond which no
    feature data should be read regardless of any per-row window. Column
    names always win the ambiguity — a column named e.g. "2025-10-31" would
    be a pathological form, not a real one.

    Blank is a third, OPTIONAL shape: no cutoff at all. Returns `None` for the
    caller to skip the temporal predicate (REJECTION 2) entirely rather than
    resolve a column that does not exist.
    """
    if not feature_cutoff:
        return cohort, None

    if feature_cutoff in cohort.columns:
        return cohort, feature_cutoff

    cutoff = pd.to_datetime(feature_cutoff, utc=True, errors="coerce")
    if pd.isna(cutoff):
        raise PitJoinError(
            f"point_in_time.feature_cutoff is {feature_cutoff!r}, which is neither a "
            f"column the cohort carries ({sorted(cohort.columns)}) nor a parseable "
            f"date literal."
        )
    _log(
        "feature_cutoff resolved to a literal date shared by every row",
        runId=run_id,
        featureCutoff=feature_cutoff,
    )
    cohort = cohort.copy()
    cohort[LITERAL_CUTOFF_COLUMN] = cutoff
    return cohort, LITERAL_CUTOFF_COLUMN


# --- the two rejections ------------------------------------------------------


def reject_target_derived(features, point_in_time, join_keys, event_time, group_name):
    """REJECTION 1 — drop every column named in `disallowed_feature_names`.

    Independent of the temporal check: a feature computed from the outcome is leaky
    whenever it arrived, so no `event_time` makes it acceptable.

    Dropped BEFORE the join, not checked after it. The cohort may already carry a
    same-named column of its own (its `days_in_study` maturity duration, say) that is
    NOT the banned M2 feature; filtering post-join could operate on whichever side
    pandas renamed through merge's suffixes, while dropping pre-join removes the
    banned column at source so no such collision can arise for it at all.

    The join keys and the event-time column are exempt — dropping a column the join
    runs ON would break the join rather than prevent leakage.

    Returns `(features, report_entry)`.
    """
    configured = list(point_in_time.get("disallowed_feature_names") or [])
    enabled = bool(point_in_time.get("disallow_target_derived_features", True))
    entry = {
        "check": "target_derived_features",
        "feature_group": group_name,
        "enabled": enabled,
        "configured": configured,
        "rejected_columns": [],
    }
    if not enabled:
        return features, entry

    banned = set(configured) - set(join_keys) - {event_time}
    rejected = sorted(column for column in features.columns if column in banned)
    entry["rejected_columns"] = rejected
    if rejected:
        features = features.drop(columns=rejected)
    return features, entry


def join_key_values(frame, side, field, run_id):
    """One join key side's values, with its ordered `transformations[]` applied.

    Returns a Series — deliberately NOT a mutated `frame`. Each step's output replaces
    the key FOR THIS JOIN ONLY, so the transformed values go into a temporary key
    column the caller drops afterwards and the authored column survives untouched in
    the output. Used when one side's join column needs a cast to match the other (a raw
    date string against an ISO month key).
    """
    column = side["column"]
    single = frame[[column]]
    for index, block in enumerate(side.get("transformations") or []):
        step_field = f"{field}.transformations[{index}]"
        single = engine_dispatch.call_column(block, step_field, single)
        _log(
            "join key transformed",
            runId=run_id,
            field=step_field,
            column=column,
            transformation=engine_dispatch.describe(block),
        )
    return single.iloc[:, 0].to_numpy()


def pit_join_group(
    cohort,
    features,
    group,
    event_time,
    cutoff_column,
    run_id,
    original_columns,
    pushdown=None,
):
    """REJECTION 2 — the temporal predicate, then the as-of attach for one group.

    Per join key, keep the LATEST feature row whose `event_time` is at or before that
    row's own `feature_cutoff` value, then join it back per the group's `join_type`.

    `join_type` is the group's own choice (§4, default `left`):

      - `left` retains a key with no qualifying feature row, with NULL features. An
        entity the store has nothing for is a fact about the store, not a reason to
        lose the labelled row, so coverage is measured rather than silently dropped.
      - `inner` drops those rows instead. The loss is deliberate, so it is counted and
        reported: a cohort that shrank at the join is the author's decision, not
        something a reader should have to infer from a row count two stages later.

    `right` and `outer` are refused, not supported: either would attach feature-group
    rows with no matching dataset row, widening the dataset past the cohort assembled
    upstream and voiding this stage's own guarantees.

    The predicate runs against the cohort's `feature_cutoff` COLUMN, per row — not a
    single run-wide date. Masking puts `observation_window_end` (the masking-aware
    cutoff) months away from `anchor_date`, so one global cutoff would let masked
    rows read past their own.

    `cutoff_column` is `None` when `point_in_time.feature_cutoff` was left blank
    (OPTIONAL) — the predicate is skipped entirely rather than compared against a
    column that was never resolved, so every row/feature match is taken regardless
    of event_time. The report's `enabled` flag records this rather than leaving a
    reader to infer "no rows rejected" from a suspiciously-zero count.

    `join_keys` is a list because a group may be keyed finer than the entity (the
    monthly-panel case: entity + month). Both the merge and the as-of de-duplication
    run on the whole key — de-duplicating on the entity alone when the group is one
    row per entity-month would collapse every month into one and attach the wrong
    period's features.

    COLLISIONS AGAINST THE ORIGINAL COHORT (`original_columns`) still raise: a
    feature literally named e.g. `patient_id` is almost certainly a real naming
    mistake, not a legitimate multi-group scenario. COLLISIONS AGAINST AN
    ALREADY-ATTACHED FEATURE COLUMN (from an earlier group in
    `point_in_time.feature_groups`) instead DROP the second group's colliding
    column and keep the first group's — confirmed as a real, recurring shape
    (dx_pat_mn and px_pat_mn's `${rx}`-expanded per-category feature names
    collided on ~2,275 columns, both derived from the same underlying
    EVENT_GROUP naming convention) rather than a rare edge case worth failing
    the whole run over.

    `pushdown` is what bounded the offline read, reported because it changes what the
    counts below can mean: a feature row excluded at source was never read, so it is
    not in `rejected_feature_rows`.

    Returns `(joined, feature_columns, report_entry)`.
    """
    group_name = group["name"]
    keys = group["join_keys"]
    dataset_columns = [key["dataset"]["column"] for key in keys]
    feature_columns = [key["feature_group"]["column"] for key in keys]

    # Stage 1 refuses anything else and the compiler defaults a blank to `left`, so
    # this only fires for a document written by an incompatible stage 1. It is checked
    # because the value reaches pandas' `how`, where `outer` is perfectly valid and
    # would widen the dataset past the cohort instead of failing.
    join_type = group.get("join_type") or DEFAULT_JOIN_TYPE
    if join_type not in ALLOWED_JOIN_TYPES:
        raise PitJoinError(
            f"point_in_time.feature_groups[{group_name}].join_type is {join_type!r}; "
            f"this stage joins {list(ALLOWED_JOIN_TYPES)} only."
        )

    missing = [c for c in dataset_columns if c not in cohort.columns]
    if missing:
        raise PitJoinError(
            f"point_in_time.feature_groups[{group_name}].join_keys names dataset "
            f"column(s) {missing}, which the cohort does not carry. It has "
            f"{sorted(cohort.columns)}."
        )
    missing = [c for c in feature_columns if c not in features.columns]
    if missing:
        raise PitJoinError(
            f"point_in_time.feature_groups[{group_name}].join_keys names feature "
            f"group column(s) {missing}, which the group does not store. It has "
            f"{sorted(features.columns)}."
        )

    # Temporary key columns hold the TRANSFORMED values the match runs on, under
    # reserved names, so the two sides can be compared without either side's authored
    # column being rewritten. Dropped again below: a join-key transformation reshapes
    # the key for this join only, and the dataset keeps the values the author wrote.
    key_names = [f"{KEY_COLUMN_PREFIX}{index}" for index in range(len(keys))]
    left, right = cohort.copy(), features.copy()
    for index, key in enumerate(keys):
        field = f"point_in_time.feature_groups[{group_name}].join_keys[{index}]"
        left[key_names[index]] = join_key_values(
            cohort, key["dataset"], f"{field}.dataset", run_id
        )
        right[key_names[index]] = join_key_values(
            features, key["feature_group"], f"{field}.feature_group", run_id
        )

    attached = [
        column
        for column in features.columns
        if column not in set(feature_columns) | {event_time}
    ]
    all_collisions = set(attached) & set(cohort.columns)
    original_collisions = sorted(all_collisions & set(original_columns))
    prior_group_collisions = sorted(all_collisions - set(original_columns))
    if prior_group_collisions:
        _log(
            "dropping feature group's colliding column(s), keeping the earlier group's",
            runId=run_id,
            featureGroup=group_name,
            droppedColumns=prior_group_collisions,
        )
        attached = [
            column for column in attached if column not in prior_group_collisions
        ]
    if original_collisions:
        raise PitJoinError(
            f"feature group {group_name} would attach column(s) {original_collisions}, "
            f"which the cohort already carries — the join would silently rename one "
            f"side through merge's suffixes. Add the name(s) to "
            f"point_in_time.disallowed_feature_names, or name a narrower "
            f"feature_columns list."
        )

    # Compared as timestamps on both sides: Athena returns the event time as text and
    # the cohort's cutoff is a date, so an unparsed comparison would be lexical.
    probe = left[key_names].copy()
    # `cutoff_column` is None when `point_in_time.feature_cutoff` was left blank — the
    # temporal predicate is skipped entirely rather than compared against a column
    # that does not exist, so every matched row is taken as-of regardless of its
    # event_time.
    if cutoff_column is not None:
        probe[CUTOFF_COLUMN] = pd.to_datetime(
            cohort[cutoff_column], utc=True, errors="coerce"
        ).to_numpy()
    merged = probe.merge(
        right[key_names + [event_time] + attached], on=key_names, how="left"
    )
    merged[EVENT_TIME_COLUMN] = pd.to_datetime(
        merged[event_time], utc=True, errors="coerce"
    )

    matched = merged[EVENT_TIME_COLUMN].notna()
    if cutoff_column is None:
        in_time = matched
    else:
        in_time = matched & (merged[EVENT_TIME_COLUMN] <= merged[CUTOFF_COLUMN])
    rejected_rows = int((matched & ~in_time).sum())

    as_of = (
        merged[in_time]
        .sort_values(EVENT_TIME_COLUMN)
        .drop_duplicates(subset=key_names, keep="last")
    )
    # The merge indicator carries coverage instead of the feature group's own
    # event-time column: two groups both registering `event_time` would collide with
    # each other on the second join, and the picked row's timestamp is not something a
    # later stage reads.
    joined = left.merge(
        as_of[key_names + attached],
        on=key_names,
        how=join_type,
        indicator=MATCHED_COLUMN,
    )
    covered = int((joined[MATCHED_COLUMN] == "both").sum())
    joined = joined.drop(columns=key_names + [MATCHED_COLUMN])
    # Only `inner` can lose rows here. Reported either way so the number is present
    # rather than implied by the difference between two other counts.
    dropped = int(len(left) - len(joined))
    entry = {
        "check": "temporal_point_in_time",
        "feature_group": group_name,
        "enabled": cutoff_column is not None,
        "predicate": (
            f"feature.{event_time} <= dataset.{cutoff_column}"
            if cutoff_column is not None
            else None
        ),
        "cutoff_column": cutoff_column,
        "join_type": join_type,
        "join_keys": [
            {"dataset": d, "feature_group": f}
            for d, f in zip(dataset_columns, feature_columns)
        ],
        "offline_rows_read": int(len(features)),
        "read_pushdown": pushdown or {},
        "rejected_feature_rows": rejected_rows,
        "rows_with_features": covered,
        "rows_without_features": int(len(joined) - covered),
        "rows_dropped_no_match": dropped,
        "attached_columns": attached,
    }
    return joined, attached, entry


def build_report(intake, frame, columns, rejections, generated_at):
    """The PIT-join report — this stage's audit artifact.

    The two rejections are separate entries per feature group, never merged into one
    count: AC-3 requires the target-derived rejection to be reported independently of
    the temporal one, because a reader has to be able to tell which rule fired.
    """
    point_in_time = intake["point_in_time"]
    return {
        "run_id": intake["run_id"],
        "model_name": intake["model_name"],
        "generated_at": generated_at,
        "source_location": intake["source"]["source_location"],
        "upstream_cohort_label_run_id": intake["upstream"].get("cohort_label_run_id"),
        "feature_cutoff": point_in_time["feature_cutoff"],
        "rows": int(len(frame)),
        "columns": columns,
        "feature_groups": [g["name"] for g in point_in_time["feature_groups"]],
        "rejections": rejections,
    }


def read_cohort(s3, secrets, config, source, run_id):
    """The labelled cohort, from whichever of the three modes the form selected.

    `source_location` fully drives the read. The execution input's
    `population_labeled.parquet` URI is lineage only and travels on `upstream` — with
    `s3` and a blank prefix the two coincide, which is exactly why it is not read
    directly here (handoff §4.5).
    """
    location = source["source_location"]
    if location == "s3":
        return read_s3_cohort(s3, source["s3_uri"], config.account_id, run_id)
    if location == "snowflake":
        return read_snowflake_cohort(s3, secrets, config, source, run_id)
    if location == "feature_group":
        group = source["feature_group"]
        athena = boto3.client("athena", region_name=config.region)
        frame, _, _ = read_offline_features(
            athena,
            s3,
            config,
            group["feature_group_name"],
            group.get("feature_columns") or [],
            # No join columns: here the group IS the cohort, not something joined to it.
            [],
            run_id,
        )
        # Unbounded deliberately: there is no cohort to bound it by yet — this group is
        # the cohort.
        return frame
    raise PitJoinError(
        f"source.source_location: {location!r} is not one of s3, snowflake, "
        f"feature_group."
    )


def main(argv=None):
    args = _parse_args(argv)
    config = job_config.load()
    run_id = args.run_id

    s3 = boto3.client("s3", region_name=config.region)
    secrets = boto3.client("secretsmanager", region_name=config.region)
    athena = boto3.client("athena", region_name=config.region)

    intake = parse_intake(
        s3_io.read_text(
            s3, args.compiled_intake_s3_uri, "compiled_intake_s3_uri", config.account_id
        )
    )
    # The run id is the first segment of the object key, so a mismatch means this run
    # was handed another run's document.
    if intake["run_id"] != run_id:
        raise PitJoinError(
            f"run_id: argument {run_id!r} does not match the compiled intake's "
            f"{intake['run_id']!r}."
        )
    if intake["env"] != config.env:
        raise PitJoinError(
            f"env: the compiled intake was compiled for {intake['env']!r} but this "
            f"job runs in {config.env!r}."
        )

    source = intake["source"]
    point_in_time = intake["point_in_time"]
    assembly = intake["dataset_assembly"]
    # source.record_id, not the upstream population.entity_id_column: this form names
    # the identifier on the dataset it reads. Stage 1 ERRORs if the two disagree, so
    # they are the same column by the time this runs.
    entity_id = source["record_id"]

    cohort = read_cohort(s3, secrets, config, source, run_id)
    _log(
        "cohort read",
        runId=run_id,
        modelName=intake["model_name"],
        sourceLocation=source["source_location"],
        rows=len(cohort),
        columns=list(cohort.columns),
    )
    if entity_id not in cohort.columns:
        raise PitJoinError(
            f"source.record_id is {entity_id!r} but the cohort does not carry that "
            f"column. It has {sorted(cohort.columns)}."
        )

    cohort = apply_dedup(cohort, assembly.get("dedup_rule"), entity_id, run_id)
    cohort = apply_cohort_cap(cohort, assembly.get("max_cohort_size"), run_id)

    cohort, cutoff_column = resolve_cutoff_column(
        cohort, point_in_time["feature_cutoff"], run_id
    )

    original_columns = set(cohort.columns)

    # The latest cutoff in the cohort bounds every group's offline read (see
    # `offline_store_sql`). None where `point_in_time.feature_cutoff` was left blank
    # (OPTIONAL): there is no column to take a maximum from, and the per-row temporal
    # predicate is skipped for the same reason, so the read is bounded by the join keys
    # alone. Also None where nothing parsed, which bounds nothing either.
    max_cutoff = None
    if cutoff_column is not None:
        max_cutoff = pd.to_datetime(
            cohort[cutoff_column], utc=True, errors="coerce"
        ).max()
        if pd.isna(max_cutoff):
            max_cutoff = None

    # WHETHER THE AS-OF PICK CAN BE PUSHED INTO THE QUERY. It can exactly when every row
    # shares one cutoff, because then the bound the query applies IS each row's own
    # cutoff and the latest row per key before it is the row the join would have picked.
    # Blank `feature_cutoff` qualifies (no temporal predicate at all) and so does a
    # literal date (`resolve_cutoff_column` gives every row the same value).
    #
    # A PER-ROW cutoff does not: the query can only bound by the cohort's maximum, and
    # the latest row before that maximum may be after some individual row's own cutoff —
    # whose correct answer is an earlier row the query would already have dropped. Those
    # forms keep the pandas path, which sees every candidate and can pick per row.
    uniform_cutoff = cutoff_column is None or (
        cohort[cutoff_column].nunique(dropna=False) <= 1
    )
    _log(
        "as-of pick placement decided",
        runId=run_id,
        cutoffColumn=cutoff_column,
        uniformCutoff=bool(uniform_cutoff),
        where="athena" if uniform_cutoff else "pandas",
        reason=(
            "every row shares one cutoff, so the query's bound is each row's own"
            if uniform_cutoff
            else "cutoff varies per row; only pandas sees every candidate row"
        ),
    )

    joined, rejections, attached_total = cohort, [], []
    for group in point_in_time["feature_groups"]:
        group_name = group["name"]
        # The feature-group side of every join key: added to the projection because the
        # join needs it, and exempt from the target-derived drop because removing a
        # column the join runs on would break the join rather than prevent leakage.
        feature_keys = [key["feature_group"]["column"] for key in group["join_keys"]]
        # Only the keys neither side transforms bound the read: a transformed key's
        # values are not what the stored column holds (`offline_store_sql`). A key
        # naming a column the cohort lacks is left to the join, which says so properly.
        key_values = {
            key["feature_group"]["column"]: cohort[key["dataset"]["column"]]
            for key in group["join_keys"]
            if key["dataset"]["column"] in cohort.columns
            and not (
                key["dataset"].get("transformations")
                or key["feature_group"].get("transformations")
            )
        }
        features, event_time, pushdown = read_offline_features(
            athena,
            s3,
            config,
            group_name,
            group.get("feature_columns") or [],
            feature_keys,
            run_id,
            key_values=key_values,
            max_event_time=max_cutoff,
            # EVERY join key, not `key_values`. The pick must partition by exactly what
            # the join de-duplicates on (`pit_join_group`'s `key_names`, one per
            # join_keys entry); partitioning by fewer would keep one row per SUBSET of
            # the key and silently discard rows the join needed — a panel group whose
            # month_id is transformed would collapse to one month per entity.
            #
            # `key_values` is the wrong set precisely because it drops transformed keys,
            # and it is safe to include them here: ranking within the RAW stored column
            # is a superset of ranking within its transformed value. A many-to-one
            # transformation puts several raw keys in one transformed key, the query
            # keeps the latest row of each, and pandas then picks the latest of those —
            # which is the latest overall.
            as_of_keys=feature_keys if uniform_cutoff else None,
        )

        features, target_derived = reject_target_derived(
            features, point_in_time, feature_keys, event_time, group_name
        )
        rejections.append(target_derived)
        _log(
            "target-derived rejection applied",
            runId=run_id,
            featureGroup=group_name,
            rejectedColumns=target_derived["rejected_columns"],
            enabled=target_derived["enabled"],
        )

        joined, attached, temporal = pit_join_group(
            joined,
            features,
            group,
            event_time,
            cutoff_column,
            run_id,
            original_columns,
            pushdown,
        )
        rejections.append(temporal)
        attached_total += attached
        _log(
            "temporal rejection applied and features attached",
            runId=run_id,
            featureGroup=group_name,
            predicate=temporal["predicate"],
            joinType=temporal["join_type"],
            rejectedFeatureRows=temporal["rejected_feature_rows"],
            rowsWithFeatures=temporal["rows_with_features"],
            rowsWithoutFeatures=temporal["rows_without_features"],
            rowsDroppedNoMatch=temporal["rows_dropped_no_match"],
            attachedColumns=attached,
        )

    columns = list(joined.columns)
    generated_at = utc_now()
    report = build_report(intake, joined, columns, rejections, generated_at)

    buffer = io.BytesIO()
    joined.to_parquet(buffer, index=False)
    output_prefix = intake["jobs"]["pit_join"]["output_s3_uri"].rstrip("/") + "/"

    written = [
        s3_io.put_object(
            s3,
            output_prefix + PARQUET_OBJECT,
            buffer.getvalue(),
            "application/octet-stream",
            "jobs.pit_join.output_s3_uri",
            config,
        ),
        s3_io.put_object(
            s3,
            output_prefix + REPORT_OBJECT,
            json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
            "application/json",
            "jobs.pit_join.output_s3_uri",
            config,
        ),
    ]

    _log(
        "point-in-time join complete",
        runId=run_id,
        rows=len(joined),
        featuresAttached=attached_total,
        written=written,
    )
    return report


if __name__ == "__main__":
    main()
