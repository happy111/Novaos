"""Feature transform + ingestion — stage 4 of the feature-ingestion pipeline.

The SageMaker Processing job of AR-05-e. Runs as `FeatureStore-IngestRole` (AC-4),
reads Gold with the point-in-time-clipped SQL stage 1 wrote, applies each
feature's declared transformation, `PutRecord`s into the feature group, and
writes the ingest report.

    in:  --compiled-intake-s3-uri   the contract stage 1 wrote — the same object
                                    stages 2 and 3 read
         --run-id                   the run id the state machine minted
    out: records in the online store, the offline store appended by Feature Store
         on its own schedule, and an ingest report JSON at ingest.output_s3_uri

Nothing is re-derived from the YAML intake form: the compiled intake is the
contract between stages, and never re-parsed.

Identity columns, feature columns and duplicate keys are all checked before the
first write. Records are then built and written a slice of rows at a time, with
the NEXT slice built while the current one's writes are in flight, so peak memory
is two slices however many rows the query returned. Building a record set is pure
Python and holds one core; the writes are network-bound and release the GIL, so
overlapping them makes the two cost the larger rather than the sum — measured at
7.1s per 50k slice, 4.3s of it building. Each slice's writes fan out over one
run-long thread pool, with transient errors retried.

TRANSFORM DISPATCH — there is NO REGISTRY. Each `ingest.transforms[i].transformation`
is a `{module, function, source_columns, params}` block (`intake_compiler.
transform_map()`), resolved by `ar_transformations.utils.dispatch.
call_transformation` — the same dispatch contract the launcher-side validator
(`intake_schema.check_dispatch`) checked each block against, so a form that
validates also runs. Every feature is computed COLUMN-WISE, before the first
PutRecord: `call_transformation` takes the whole source DataFrame and returns
one output column, matching the engine's contract
`def fn(inputs: dict[str, pd.DataFrame], **params) -> pd.DataFrame`. Adding a
transformation is writing a module in the `ar_transformations` distribution; it
is never editing a lookup table in here.

No Snowflake connection detail appears in the compiled intake. It arrives as
environment variables carrying secret IDs, never secret values (SPEC §6.5).
"""

import argparse
import importlib
import inspect
import json
import math
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import numpy as np
import pandas as pd
from botocore.config import Config
from botocore.exceptions import ClientError

from src.common import compiled_intake
from src.common import config as config_module
from src.common import feature_group, intake_schema
from src.common.intake_compiler import resolve_dynamic_templates, utc_now

FEATURE_STORE_RUNTIME = "sagemaker-featurestore-runtime"
REPORT_OBJECT_PREFIX = "ingest_report_"
# Loop reports periodically at most PROGRESS_REPORTS ti
# so a million records is twenty lines rather than twenty thousand.
PROGRESS_EVERY = 50
PROGRESS_REPORTS = 20

# Records are built and written one slice of rows at a time
ROW_SLICE = 50_000

# Records go through BatchWriteRecord
PUT_WORKERS = 50
PUT_CHUNK_SIZE = 25
PUT_RETRIES = 3
PUT_BACKOFF_SECONDS = 0.5
# ConflictException: two threads writing the same record key — safe to retry.
TRANSIENT_ERROR_CODES = frozenset(
    {
        "InternalFailure",
        "ServiceUnavailable",
        "ThrottlingException",
        "ConflictException",
    }
)
# The report is an S3 object, so the failure list is sampled rather than unbounded.
WRITE_ERROR_SAMPLE = 20

# What PutRecord's EventTime validation accepts.
EVENT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Delivered by the state machine from the `snowflake` block in
# environments/<env>.json. The two secret variables hold IDs, never values.
SNOWFLAKE_ENV_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_HOST",
    "SNOWFLAKE_USER_SECRET_ID",
    "SNOWFLAKE_PRIVATE_KEY_SECRET_ID",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
    "SNOWFLAKE_ROLE",
)
SNOWFLAKE_WAREHOUSE_PATTERN_VAR = "SNOWFLAKE_WAREHOUSE_PATTERN"
WAREHOUSE_SIZE_PLACEHOLDER = "{size}"
# The connection secret is a JSON document; this is the field holding the login.
SNOWFLAKE_USER_FIELD = "snow_user"
SNOWFLAKE_USER_DOMAIN = "@NOVARTIS.NET"
# A login that hangs would otherwise burn the job's whole runtime.
SNOWFLAKE_LOGIN_TIMEOUT_SECONDS = 30
# Matches this Processing job's own MaxRuntimeInSeconds (environments/<env>.json's
# state_machines.feature_onboarding.ingest.max_runtime_seconds) exactly — a longer
# query timeout would never fire; SageMaker would kill the job first.
SNOWFLAKE_QUERY_TIMEOUT_SECONDS = 86400


class IngestError(Exception):
    """The job cannot run: a malformed argument, an unusable compiled intake, or
    source data that does not match what the form declared."""


def _log(message, **fields):
    """Structured log line. runId is on every line, per X5."""
    print(json.dumps({"message": message, **fields}, default=str))


def _split_s3_uri(uri, field):
    if not str(uri or "").startswith("s3://"):
        raise IngestError(f"{field}: {uri!r} must be an s3:// URI.")
    bucket, _, key = uri[len("s3://") :].partition("/")
    if not bucket or not key:
        raise IngestError(f"{field}: {uri!r} must include a bucket and a key.")
    return bucket, key


def _read_s3_text(s3, uri, field, account_id):
    """Read an S3 object, refusing a bucket this account does not own."""
    bucket, key = _split_s3_uri(uri, field)
    response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account_id)
    return response["Body"].read().decode("utf-8")


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Transform Gold and ingest features.")
    parser.add_argument("--compiled-intake-s3-uri", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if not intake_schema.is_valid_run_id(args.run_id):
        raise IngestError(
            f"run_id: {args.run_id!r} must be 'r' followed by 12 lowercase base32 "
            "characters, e.g. r5f3a9c2d1e0b."
        )
    return args


def snowflake_settings():
    """Every Snowflake variable, naming all the missing ones at once."""
    missing = [name for name in SNOWFLAKE_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise config_module.ConfigError(
            "missing required environment variable(s): " + ", ".join(missing)
        )
    settings = {name: os.environ[name] for name in SNOWFLAKE_ENV_VARS}
    settings[SNOWFLAKE_WAREHOUSE_PATTERN_VAR] = os.environ.get(
        SNOWFLAKE_WAREHOUSE_PATTERN_VAR, ""
    )
    return settings


def snowflake_warehouse(settings, ingest):
    """The warehouse to connect to: the size the form declared, else the environment's.

    A declared size is not silently downgraded — if the environment carries no naming
    pattern the job stops, because connecting to the default warehouse would ignore a
    sizing decision the form made deliberately.
    """
    size = str(ingest.get("warehouse_size") or "").strip()
    if not size:
        return settings["SNOWFLAKE_WAREHOUSE"]
    pattern = settings.get(SNOWFLAKE_WAREHOUSE_PATTERN_VAR)
    if not pattern:
        raise IngestError(
            f"ingest.warehouse_size is {size!r} but {SNOWFLAKE_WAREHOUSE_PATTERN_VAR} "
            "is not set for this environment, so the warehouse name cannot be composed."
        )
    return pattern.replace(WAREHOUSE_SIZE_PLACEHOLDER, size)


def connect_to_snowflake(secrets, settings, warehouse):
    """Key-pair auth with the PEM fetched from Secrets Manager (SPEC §6.5).

    The connector and cryptography are imported here rather than at module scope
    so the unit tests, which never connect, do not need either installed.
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
        warehouse=warehouse,
        database=settings["SNOWFLAKE_DATABASE"],
        schema=settings["SNOWFLAKE_SCHEMA"],
        role=settings["SNOWFLAKE_ROLE"],
        login_timeout=SNOWFLAKE_LOGIN_TIMEOUT_SECONDS,
        network_timeout=SNOWFLAKE_QUERY_TIMEOUT_SECONDS,
        session_parameters={
            "STATEMENT_TIMEOUT_IN_SECONDS": SNOWFLAKE_QUERY_TIMEOUT_SECONDS
        },
    )


def read_source(connection, sql):
    """Run the clipped query. Columns are lowercased.

    Snowflake returns unquoted identifiers upper-cased, while every column a
    form names is written lower-case, so the fetch is normalised once here.

    The two lines are not the same kind of work and do not fail the same way.
    `execute()` runs server-side and is what Snowflake's QUERY_HISTORY records.
    `fetch_pandas_all()` is client-side: a large result comes back as one inline
    batch plus a chunk per ~45k rows on Snowflake's S3 result stage (4.17M rows =
    96 chunks), each fetched over a fresh HTTPS connection that the connector
    OCSP-validates. With no route to the responder those checks stall ~45s per
    chunk and the whole read takes hours while QUERY_HISTORY still shows SUCCESS
    in 40s — see the proxy split in environments/<env>.json snowflake.env_vars.
    A small result is entirely inline and never touches the stage, which is why a
    few-hundred-row form can pass on a path where a real one hangs.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        frame = cursor.fetch_pandas_all()
    finally:
        cursor.close()
    frame.columns = [str(name).lower() for name in frame.columns]
    return frame


def validate_identity_columns(columns, ingest):
    """The record identifier and event time, checked before anything else.

    Named apart from the feature source columns because without these two no
    record can be built at all — every other column is one feature's problem.
    """
    present = set(columns)
    for field, column in (
        ("record_identifier_feature_name", ingest["record_identifier_feature_name"]),
        ("event_time_feature_name", ingest["event_time_feature_name"]),
    ):
        if column not in present:
            raise IngestError(
                f"ingest.{field} {column!r} was not returned by the source query. "
                f"Returned: {sorted(present)}."
            )


def missing_columns(columns, ingest):
    """Declared columns the source query did not return — a typo or schema drift.

    A `dynamic_templates` entry's `source_columns` is checked too: every
    value there is already a real column name, including the column-derived
    var's own entry (`column_vars` just points back into this same dict) —
    resolving the template only substitutes the var's real VALUES, never its
    column binding, so this check needs no template resolution to run.
    """
    declared = {
        ingest["record_identifier_feature_name"],
        ingest["event_time_feature_name"],
    }
    for transform in ingest["transforms"]:
        source_columns = transform["transformation"].get("source_columns") or {}
        declared.update(str(c) for c in source_columns.values() if c)
    for template in ingest.get("dynamic_templates") or []:
        source_columns = template.get("transformation", {}).get("source_columns") or {}
        declared.update(str(c) for c in source_columns.values() if c)
    return sorted(declared - set(columns))


def missing_features(computed, ingest):
    """Declared features no transformation produced a column for."""
    produced = set(computed.columns)
    return sorted(
        {t["feature"] for t in ingest["transforms"] if t["feature"] not in produced}
    )


def deduplicate(df, ingest):
    """Collapse rows sharing a (record identifier, event time) key, last one wins.

    A source query that UNIONs branches can emit the key twice; left in, the two
    rows race on the same online-store key and PutRecord raises ConflictException.

    Aggregating pivot transforms' wide results are computed over the full frame
    (see `_wide_results` in main()) BEFORE this runs, so collapsing to one row per
    key here does not strip the rows they aggregate over; the deduped frame is the
    record grain the non-pivot features and the write use.
    """
    key_columns = [
        ingest["record_identifier_feature_name"],
        ingest["event_time_feature_name"],
    ]
    deduplicated = df.drop_duplicates(subset=key_columns, keep="last").reset_index(
        drop=True
    )
    return deduplicated, len(df) - len(deduplicated)


def resolve_transformation(block, field):
    """The callable a block names.

    Both the `ar_transformations` distribution and a form's own
    `local_transformations.<name>` module are plain-importable here: unlike the
    Snowflake-source stages, this pipeline's Processing job takes no
    `ProcessingInputs` channel at all (every object is fetched with boto3, per
    this module's docstring), so a form-local module is instead BAKED into the
    image at `/novos/local_transformations/` by `build_and_push.sh` — the same
    convention the cohort-label image uses for its own form-local tier.
    """
    module_name = str(block["module"]).strip()
    function_name = str(block["function"]).strip()

    if not intake_schema.is_dotted_module_path(module_name):
        raise IngestError(
            f"{field}.module {module_name!r} must be a dotted Python module "
            f"path (letters, digits, underscores, separated by '.') — the "
            f"launcher-side validator should have caught this before the "
            f"compiled intake reached this job."
        )

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise IngestError(
            f"{field}.module {module_name!r} is not importable inside the "
            f"Processing image ({exc}). The transformations distribution is "
            f"installed from requirements.txt, and a form-local module is baked "
            f"in by build_and_push.sh — if this module was added recently, "
            f"rebuild and push the image."
        ) from exc

    func = getattr(module, function_name, None)
    if func is None:
        available = sorted(
            n for n in getattr(module, "__all__", dir(module)) if not n.startswith("_")
        )
        raise IngestError(
            f"{field}.function {function_name!r} does not exist in "
            f"{module_name!r}. Available: {available}"
        )
    return func


def _match_key_columns(result_columns, canonical_names):
    """Case-insensitive lookup of `canonical_names` among `result_columns`.

    Returns `{canonical_name: actual_result_column}`, or None if
    `canonical_names` is empty or any name in it has no case-insensitive
    match — a local transformation picks its own casing for a key column
    (the sample scripts hardcode "PATIENT_ID"; the real Gold frame has it
    lowercased), so matching is case-insensitive rather than exact. Empty
    input never counts as a match: an ingest document that carries no
    identity columns (a bare unit-test fixture, mainly) must fall through to
    the row-aligned shape, not merge on zero keys.
    """
    canonical_names = [n for n in canonical_names if n]
    if not canonical_names:
        return None
    lower_to_actual = {str(c).lower(): c for c in result_columns}
    mapping = {}
    for name in canonical_names:
        actual = lower_to_actual.get(str(name).lower())
        if actual is None:
            return None
        mapping[name] = actual
    return mapping


def _select_value_column(result, feature, key_columns_actual, field):
    """The one column of `result` holding this feature's value.

    With exactly one non-key column, it is unambiguous regardless of its
    name — most local transformations return exactly this shape (see
    feature-intake-form.yaml §5 Pattern B). With more than one (a Pattern C
    dispatch shared across several resolved features, e.g. one count column
    per day-window), the column named exactly `feature` (case-insensitive)
    disambiguates, per the form's own documented contract ("one of its
    columns must be named exactly as `name`").
    """
    value_columns = [c for c in result.columns if c not in key_columns_actual]
    if len(value_columns) == 1:
        return value_columns[0]
    lower_to_actual = {str(c).lower(): c for c in value_columns}
    match = lower_to_actual.get(str(feature).lower())
    if match is not None:
        return match
    raise IngestError(
        f"{field} returned {len(value_columns)} non-key column(s) "
        f"({value_columns}); none is named exactly {feature!r} to say which "
        f"one is this feature's value."
    )


def apply_transformation(
    df, block, field, feature, entity_id_columns, event_time_column
):
    """Call one feature's transformation over `df` and return its one output column.

    The engine's contract is column-wise: `fn(**inputs, **params) -> DataFrame`,
    where `inputs` maps the function's own ARGUMENT NAMES to single-column
    frames. That binding is `ar_transformations.utils.dispatch.build_inputs`,
    used here rather than reimplemented, so the launcher-side validator and this
    container agree on what a form means.

    `transformation.filters` (optional) scopes WHICH ROWS this feature is
    computed over — narrower than the source query, most useful for a
    Pattern C expansion that needs a different slice per combination (e.g.
    "only this brand's claims"). Applied BEFORE `source_columns` binding, so
    the dispatched function never sees an excluded row at all. A row the
    filter excludes gets null for this feature, reindexed back onto `df`'s
    full row set — the SAME "no value" outcome an unparseable input produces,
    so it is subject to the feature's own `nullable` declaration exactly like
    any other null (see `build_records`), not a silent drop.

    The return value can take any of the three shapes feature-intake-form.yaml
    §5 Pattern B documents:
      1. One row per entity, including the entity id column(s) — left-joined
         on the id.
      2. The identity keys (entity id and event time) — left-joined on both.
      3. Row-aligned to the (post-filter) input, no key columns — assigned
         positionally, the original and still the common case for a stateless
         built-in like `to_float`/`ratio`.
    Shape is decided by which key columns are PRESENT in the result (matched
    case-insensitively), not by row count: an aggregating local transformation
    (one row per patient, not per claim) is expected to return fewer rows than
    it was given, which is exactly what shapes 1/2 are for.
    """
    from src.common import filter_engine

    filters = block.get("filters")
    filtered = df
    if filters:
        try:
            filtered = filter_engine.apply_filters(df, filters, f"{field}.filters")
        except filter_engine.FilterError as exc:
            raise IngestError(str(exc)) from exc
        if filtered.empty:
            # A Pattern C combination the source data structurally never
            # satisfies (e.g. a category-derived eq-leaf ANDed with a static
            # filter neither row ever both matches) — not an error, the same
            # "no value" outcome an excluded row already produces. The
            # dispatched function is never called: with zero input rows it
            # has nothing to derive even its own output column's name from
            # (see compute_event_count's per-category naming), so calling it
            # would fail for a reason that has nothing to do with the
            # feature's own correctness.
            return pd.Series(index=df.index, dtype="object").reindex(df.index)

    result = _dispatch_transformation(filtered, block, field)
    values = _align_transformation_result(
        result, filtered, feature, entity_id_columns, event_time_column, field
    )

    if not filters:
        return values
    # Reindexed back onto EVERY row `df` carries, not just the filtered
    # subset: a row the filter excluded gets null for this feature, the same
    # way a row an upstream cast could not parse does — subject to `nullable`
    # at build_records(), never silently dropped from the record set.
    return values.reindex(df.index)


def _dispatch_transformation(filtered, block, field):
    """Call the resolved transformation over `filtered`, returning its raw
    result (not yet shape-validated or aligned back onto `df`)."""
    from ar_transformations.utils.dispatch import build_inputs

    func = resolve_transformation(block, field)
    source_columns = block.get("source_columns")
    if isinstance(source_columns, dict) and list(source_columns) == [
        intake_schema.SINGLE_COLUMN_ALIAS
    ]:
        argument = next(iter(inspect.signature(func).parameters))
        source_columns = {argument: source_columns[intake_schema.SINGLE_COLUMN_ALIAS]}
    try:
        inputs = build_inputs(filtered, source_columns, field_name=field)
    except ValueError as exc:
        raise IngestError(str(exc)) from exc

    params = block.get("params") or {}
    if not isinstance(params, dict):
        raise IngestError(
            f"{field}.params must be a mapping, got {type(params).__name__}."
        )
    try:
        return func(**inputs, **params)
    except TypeError as exc:
        raise IngestError(
            f"{field} could not be called: {exc}. params keys are {sorted(params)}; "
            f"`params` is passed through verbatim, so every key must be a named "
            f"parameter of the function."
        ) from exc
    except ValueError as exc:
        # A transformation validates its own bindings and raises naming the
        # ARGUMENT it wanted — that message is the useful half; the YAML field
        # is prefixed so the author knows which of several feature blocks
        # produced it, rather than reading it off a traceback.
        raise IngestError(f"{field}: {exc}") from exc


def _align_transformation_result(
    result, filtered, feature, entity_id_columns, event_time_column, field
):
    """The transformation's raw result, coerced to a DataFrame and aligned
    back onto `filtered`'s row set — shape 3 (row-aligned) or shape 1/2
    (left-joined on whichever key columns are present), per
    `apply_transformation`'s own docstring."""
    if isinstance(result, pd.Series):
        result = result.to_frame()
    if not isinstance(result, pd.DataFrame):
        raise IngestError(
            f"{field} returned {type(result).__name__}; a transformation must "
            f"return a DataFrame or a Series."
        )

    key_map = _match_key_columns(
        result.columns, list(entity_id_columns) + [event_time_column]
    ) or _match_key_columns(result.columns, list(entity_id_columns))

    if key_map is None:
        return _align_row_wise_result(
            result, filtered, entity_id_columns, event_time_column, field
        )
    return _align_keyed_result(result, filtered, feature, key_map, field)


def _align_row_wise_result(
    result, filtered, entity_id_columns, event_time_column, field
):
    """Shape 3: no key columns present, row-aligned to the filtered input."""
    if len(result) != len(filtered):
        raise IngestError(
            f"{field} returned {len(result)} row(s) for {len(filtered)} filtered "
            f"input row(s), and carries none of the identity key column(s) "
            f"({list(entity_id_columns) + [event_time_column]}) to left-join "
            f"on instead — aggregate in the source query, or return the "
            f"entity id (and optionally event time) column alongside the "
            f"value so the result can be joined back."
        )
    if result.shape[1] != 1:
        raise IngestError(
            f"{field} returned {result.shape[1]} columns; a feature is one value "
            f"per row. Declare a separate feature per output column."
        )
    return result.iloc[:, 0]


def _align_keyed_result(result, filtered, feature, key_map, field):
    """Shape 1 or 2: left-join on whichever key columns are present."""
    value_col = _select_value_column(result, feature, set(key_map.values()), field)
    canonical_keys = list(key_map)
    actual_keys = [key_map[name] for name in canonical_keys]
    keys = filtered[canonical_keys].copy()
    keys["__row__"] = filtered.index
    to_merge = result[actual_keys + [value_col]].rename(
        columns=dict(zip(actual_keys, canonical_keys))
    )
    merged = keys.merge(to_merge, on=canonical_keys, how="left")
    if len(merged) != len(filtered):
        raise IngestError(
            f"{field} returned duplicate rows for the same "
            f"{canonical_keys} — a left join must not fan a source row out "
            f"into more than one; dedupe the transformation's own output."
        )
    return merged.set_index("__row__")[value_col].reindex(filtered.index)


def _wide_cache_key(block):
    """A hashable signature for the wide (whole-column) call, so every feature a
    column-derived template expanded into shares ONE computation. Excludes the
    per-value eq filter (that is exactly what the wide call drops)."""
    return json.dumps(
        {
            "module": block.get("module"),
            "function": block.get("function"),
            "source_columns": block.get("source_columns"),
            "params": block.get("params"),
            "static_filters": block.get("static_filters"),
        },
        sort_keys=True,
        default=str,
    )


def _wide_evaluate(df, block, field, entity_id_columns=None, event_time_column=None):
    """Run the transform ONCE over `df` with only the block's STATIC filters — the
    per-value eq filter is dropped — so a category-pivoting transform sees the
    whole bound column and 0-fills across the full entity set.

    A row failing the static filter is not dropped outright: its identity
    columns (`entity_id_columns`/`event_time_column`) are kept, and every other
    bound source column is nulled. A source query that carries one row per
    cohort entity regardless of claims (a LEFT JOIN anchor row, all-null on the
    claim side for an entity with none) would otherwise have that entity's
    identity dropped along with the row once the static filter excludes it —
    so the transform never sees the entity at all and can't zero-fill it. Both
    `compute_event_count` and its siblings already build their own zero-filled
    keys from every identity row they receive; keeping the identity row alive
    here is what lets that existing, customer-owned logic reach these entities,
    without the engine ever computing or injecting a value itself.

    Returns the raw result DataFrame, or None if it cannot serve as a wide pivot.
    Any failure returns None so the caller falls back to the proven per-value
    path; a genuine error then resurfaces there with the normal message.
    """
    from ar_transformations.utils.dispatch import build_inputs

    from src.common import filter_engine

    try:
        static_filters = block.get("static_filters")
        filtered = df
        if static_filters:
            keep = filter_engine.matching_mask(df, static_filters, f"{field}.filters")
            if keep.all():
                filtered = df
            else:
                identity_columns = [
                    c for c in [*(entity_id_columns or []), event_time_column] if c
                ]
                claim_columns = [c for c in df.columns if c not in identity_columns]
                filtered = df.copy()
                filtered.loc[~keep, claim_columns] = None
            if filtered.empty:
                return None
        func = resolve_transformation(block, field)
        source_columns = block.get("source_columns")
        if isinstance(source_columns, dict) and list(source_columns) == [
            intake_schema.SINGLE_COLUMN_ALIAS
        ]:
            argument = next(iter(inspect.signature(func).parameters))
            source_columns = {
                argument: source_columns[intake_schema.SINGLE_COLUMN_ALIAS]
            }
        inputs = build_inputs(filtered, source_columns, field_name=field)
        params = block.get("params") or {}
        if not isinstance(params, dict):
            return None
        result = func(**inputs, **params)
        if isinstance(result, pd.Series):
            result = result.to_frame()
        if not isinstance(result, pd.DataFrame):
            return None
        return result
    except Exception:
        return None


def _pivot_values(wide_result, df, feature, entity_id_columns, event_time_column):
    """Pull this feature's value column out of a wide keyed result, left-joined
    back onto `df`'s record keys (aligned to df.index).

    Returns `(values, was_pivot)`:
      was_pivot  True when `wide_result` is a genuine per-value pivot — keyed on
                 the identity columns AND carrying MORE THAN ONE value column.
                 A scalar / single-value transform is not a pivot (False), so it
                 falls through to the per-value path silently, as before.
      values     this feature's Series when its column is present and joinable,
                 else None. `values is None and was_pivot` is the anomaly worth
                 logging: the family pivots, but THIS feature's column could not
                 be pulled (a name/shape mismatch) — it would silently revert to
                 the per-value path.
    """
    key_map = _match_key_columns(
        wide_result.columns, list(entity_id_columns) + [event_time_column]
    ) or _match_key_columns(wide_result.columns, list(entity_id_columns))
    if key_map is None:
        return None, False
    key_actual = set(key_map.values())
    value_columns = [c for c in wide_result.columns if c not in key_actual]
    if len(value_columns) <= 1:
        return None, False
    col = {str(c).lower(): c for c in value_columns}.get(str(feature).lower())
    if col is None:
        return None, True
    canonical_keys = list(key_map)
    actual_keys = [key_map[name] for name in canonical_keys]
    to_merge = wide_result[actual_keys + [col]].rename(
        columns=dict(zip(actual_keys, canonical_keys))
    )
    if to_merge.duplicated(subset=canonical_keys).any():
        return None, True
    keys = df[canonical_keys].copy()
    keys["__row__"] = df.index
    merged = keys.merge(to_merge, on=canonical_keys, how="left")
    return merged.set_index("__row__")[col].reindex(df.index), True


def _wide_results(df, ingest):
    """Compute, once and cached, the wide result for each column-derived pivot
    family over the WHOLE frame `df` — before any de-duplication, so an
    aggregating pivot transform (counts/flags over a record's many event rows)
    sees every row. Keyed by `_wide_cache_key`, so all the features one template
    expands into share a single computation.

    Each wide result is one row per entity (small), NOT per input row, so this
    stays flat in memory even when `df` is the full multi-million-row frame. A
    family whose wide call cannot serve as a pivot maps to None; its features
    fall back to the per-value path in `compute_features`, logged so a silent
    revert to the pre-fix behaviour is visible.
    """
    entity_id_columns = ingest.get("record_identifier_columns") or [
        c for c in [ingest.get("record_identifier_feature_name")] if c
    ]
    event_time_column = ingest.get("event_time_feature_name")

    cache = {}
    for transform in ingest["transforms"]:
        block = transform["transformation"]
        if not block.get("column_filters"):
            continue
        key = _wide_cache_key(block)
        if key in cache:
            continue
        field = f"ingest.transforms[{transform['feature']}].transformation"
        cache[key] = _wide_evaluate(
            df, block, field, entity_id_columns, event_time_column
        )
        if cache[key] is None:
            _log(
                "wide transform path unavailable — its features fall back to the "
                "per-value filter (pre-fix behaviour); check the transform",
                module=block.get("module"),
                function=block.get("function"),
            )
    return cache


def _resolve_feature_values(
    df, wide_cache, block, field, feature, entity_id_columns, event_time_column
):
    """One feature's computed values, via the wide-pivot cache when applicable,
    else the per-value `apply_transformation` path; also reports a pivot-fallback
    family (module, function) when the cache had a wide result that could not be
    pulled from it, or None when no such fallback occurred.
    """
    values = None
    fallback_family = None
    if block.get("column_filters"):
        wide_result = wide_cache.get(_wide_cache_key(block))
        if wide_result is not None:
            values, was_pivot = _pivot_values(
                wide_result, df, feature, entity_id_columns, event_time_column
            )
            if values is None and was_pivot:
                fallback_family = (block.get("module"), block.get("function"))
    if values is None:
        values = apply_transformation(
            df, block, field, feature, entity_id_columns, event_time_column
        )
    return values, fallback_family


def _log_pivot_fallbacks(pivot_fallbacks):
    """Log a summary, per (module, function) family, of how many features fell
    back from the wide-pivot path to the per-value filter.
    """
    for (module, function), count in pivot_fallbacks.items():
        _log(
            "column-derived feature(s) had a wide result but could not be pulled "
            "from it and fell back to the per-value filter (may reintroduce nulls "
            "for missing categories); check the feature name vs the transform's "
            "output column",
            module=module,
            function=function,
            featureCount=count,
        )


def compute_features(df, ingest, wide_cache=None):
    """Every declared feature, computed column-wise, before the first PutRecord.

    `df` is the RECORD-grain frame (one row per record id + event time). Non-pivot
    and row-wise features are computed on it directly — exactly as before. A
    column-derived pivot feature (a `dynamic_templates` `${var}` expansion, marked
    by `transformation.column_filters`) is instead read from `wide_cache`, whose
    entries `_wide_results` computed over the full pre-dedup frame and which is
    joined back onto `df` here — so an aggregating pivot 0-fills the full entity
    set instead of nulling every entity outside a one-value slice, while `computed`
    itself is only ever built at the (small) record grain. When a feature is not
    such a pivot it falls back to the exact per-value `apply_transformation` path.

    `wide_cache` defaults to being built from `df` — the standalone/test path,
    where `df` is not pre-deduplicated so computing the wide results from it is
    correct. The pipeline passes a cache built from the full frame.

    Returns a DataFrame indexed like `df`, one column per feature. Built in a
    single construction (not column-by-column) to avoid frame fragmentation.
    """
    if wide_cache is None:
        wide_cache = _wide_results(df, ingest)
    entity_id_columns = ingest.get("record_identifier_columns") or [
        c for c in [ingest.get("record_identifier_feature_name")] if c
    ]
    event_time_column = ingest.get("event_time_feature_name")

    columns = {}
    lineage = {}
    # A column-derived feature whose family DID produce a wide result but whose own
    # column could not be pulled from it (a name/shape mismatch, e.g. the feature
    # name and the transform's output column disagree) silently drops to the
    # per-value path — the pre-fix behaviour for that one feature. Count those per
    # family and log a summary, so a partial silent regression is visible.
    pivot_fallbacks = defaultdict(int)
    for transform in ingest["transforms"]:
        feature = transform["feature"]
        block = transform["transformation"]
        field = f"ingest.transforms[{feature}].transformation"
        values, fallback_family = _resolve_feature_values(
            df, wide_cache, block, field, feature, entity_id_columns, event_time_column
        )
        if fallback_family is not None:
            pivot_fallbacks[fallback_family] += 1
        columns[feature] = values
        lineage[feature] = f"{block['module']}.{block['function']}"
    _log_pivot_fallbacks(pivot_fallbacks)
    computed = (
        pd.DataFrame(columns, index=df.index)
        if columns
        else pd.DataFrame(index=df.index)
    )
    return computed, lineage


def _event_time_str(value):
    """Feature Store's EventTime, as the ISO-8601-with-time string it requires.

    A Snowflake column cast with TO_DATE() (this pipeline's SQL stages
    routinely do, to satisfy the PIT-window arithmetic upstream) fetches as a
    bare `datetime.date`, whose str() is "yyyy-MM-dd" — no time component.
    PutRecord's EventTime validation requires
    yyyy-MM-dd'T'HH:mm:ss(.SSS)?Z, so a date-only value is coerced to midnight
    UTC rather than passed through as-is.
    """
    return pd.Timestamp(value).strftime(EVENT_TIME_FORMAT)


def _event_time_strings(values):
    """`_event_time_str` over a whole column, null where the value is null.

    Falls back to converting value by value for any column pandas will not take
    wholesale
    """
    try:
        converted = pd.to_datetime(values)
        if pd.api.types.is_datetime64_any_dtype(converted):
            return converted.dt.strftime(EVENT_TIME_FORMAT)
    except (ValueError, TypeError):
        pass
    return values.map(lambda v: None if pd.isna(v) else _event_time_str(v))


def _as_feature_type(column, feature, feature_types):
    """A column rendered as the FeatureType the feature group was created with.

    An Integral feature must not reach the API as "2.0". A Snowflake NUMBER
    column with any null comes back float64 — the two integer columns of the
    cosentyx targeting panel are 95% and 99.9% null — and `str()` on float64
    gives a value Feature Store's Integral parser refuses outright, failing the
    whole 25-entry call. The form already declared the type, so honour it here
    rather than making every form author route integers through `to_integer`.
    """
    if feature_types.get(feature) != "Integral":
        return column
    try:
        # Int64, not int64: the nulls are rejected rows, not zeros.
        return column.astype("Int64")
    except (TypeError, ValueError) as exc:
        raise IngestError(
            f"{feature}: the form declares dtype integer, but the transformation "
            f"produced values that are not whole numbers ({exc})"
        ) from exc


def _rejection_reasons(ingest, df, computed):
    """Why each row cannot produce a record, positionally; None where it can.

    The first reason found wins, in the order the original loop checked them:
    the identity columns, then each feature in declaration order.
    """
    record_id = ingest["record_identifier_feature_name"]
    event_time = ingest["event_time_feature_name"]
    nullable = {
        t["feature"]: bool(t.get("nullable", False)) for t in ingest["transforms"]
    }
    feature_types = {t["feature"]: t.get("feature_type") for t in ingest["transforms"]}

    time_strings = _event_time_strings(df[event_time])
    # A null identifier or event time (NaT included) can produce no record at all.
    unusable = (df[record_id].isna() | pd.isna(time_strings)).tolist()
    reason = (
        f"{record_id}/{event_time}: the record identifier and event time cannot be null"
    )
    reasons = [reason if bad else None for bad in unusable]

    columns = []
    for feature in computed.columns:
        column = _as_feature_type(computed[feature], feature, feature_types)
        # Formatted per COLUMN, never per row: reading a whole row upcasts every
        # column to their common dtype.
        #
        # Only the values `build_records` will actually emit are formatted: a null
        # feature is absent from the record, so converting it is work thrown away,
        # and real panels are mostly null (measured 61-99.7% across months). The
        # slots left behind are therefore only valid where `missing` is False —
        # nothing may read `strings[position]` without checking `missing` first.
        missing = column.isna().to_numpy()
        strings = np.empty(len(column), dtype=object)
        present = ~missing
        if present.any():
            strings[present] = column[present].astype(str).to_numpy()
        if not nullable[feature] and missing.any():
            reason = (
                f"{feature}: produced no value and the form declares it not nullable"
            )
            for position, absent in enumerate(missing):
                if absent and reasons[position] is None:
                    reasons[position] = reason
        columns.append((feature, strings, missing))
    return reasons, time_strings.tolist(), df[record_id].astype(str).tolist(), columns


def build_records(ingest, df, computed, row_offset=0):
    """Every Feature Store record for the rows handed in, and every row that
    could not produce one (AC-6).

    Built column-wise: each column is formatted to strings once and then read by
    position
    """
    if df.empty:
        return [], []

    record_id = ingest["record_identifier_feature_name"]
    event_time = ingest["event_time_feature_name"]
    reasons, time_strings, id_strings, columns = _rejection_reasons(
        ingest, df, computed
    )

    records, rejected = [], []
    for position, reason in enumerate(reasons):
        if reason is not None:
            rejected.append({"row": row_offset + position, "reason": reason})
            continue
        record = [
            {"FeatureName": record_id, "ValueAsString": id_strings[position]},
            {
                "FeatureName": event_time,
                "ValueAsString": time_strings[position],
            },
        ]
        # A nullable feature with no value is simply absent from the record.
        record.extend(
            {"FeatureName": feature, "ValueAsString": strings[position]}
            for feature, strings, missing in columns
            if not missing[position]
        )
        records.append(record)
    return records, rejected


def _client_error_outcome(exc, last):
    """The error code from a `ClientError`, and whether the whole-call failure is
    transient and worth retrying (never on the last attempt).
    """
    code = exc.response.get("Error", {}).get("Code", "")
    retryable = code in TRANSIENT_ERROR_CODES and not last
    return code, retryable


def _process_batch_write_response(response, last):
    """Entries to retry from one `batch_write_record` response, the count of
    transient per-entry failures to add to the caller's retry counter (by error
    code, `"UnprocessedEntries"` included), and the error strings for entries
    that will not be retried — the write/retry/fail split for one response.
    """
    retry = list(response.get("UnprocessedEntries") or [])
    retried_delta = Counter()
    if retry:
        retried_delta["UnprocessedEntries"] += len(retry)
    errors = []
    for failure in response.get("Errors") or []:
        code = failure.get("ErrorCode") or "ValidationError"
        if code in TRANSIENT_ERROR_CODES and not last:
            retry.append(failure["Entry"])
            retried_delta[code] += 1
        else:
            errors.append(f"{code}: {failure.get('ErrorMessage', '')}".rstrip(": "))
    return retry, retried_delta, errors


def _put_chunk(runtime, fg_name, chunk):
    """One worker task: written count, the errors that survived their retries, and a
    count of the transient failures it retried through, by error code.

    The retry count is returned rather than logged or counted in a shared total: a
    worker owns nothing but its own chunk, so the caller aggregates. A silently
    successful retry is invisible otherwise, which is what makes throttling at a
    given PUT_WORKERS impossible to see — the run just slows down.
    """
    pending = [{"FeatureGroupName": fg_name, "Record": record} for record in chunk]
    errors = []
    retried = Counter()

    for attempt in range(PUT_RETRIES + 1):
        last = attempt == PUT_RETRIES
        try:
            response = runtime.batch_write_record(Entries=pending)
        except ClientError as exc:
            code, retryable = _client_error_outcome(exc, last)
            if not retryable:
                # The call failed outright, so nothing in `pending` was written.
                errors.extend(f"{code or 'ClientError'}: {exc}" for _ in pending)
                return len(chunk) - len(errors), errors, retried
            retried[code] += 1
            time.sleep(PUT_BACKOFF_SECONDS * 2**attempt)
            continue
        except Exception as exc:  # a worker must never take the pool down with it
            errors.extend(f"{type(exc).__name__}: {exc}" for _ in pending)
            return len(chunk) - len(errors), errors, retried

        # Entries absent from both lists were written; these are what remain.
        retry, retried_delta, new_errors = _process_batch_write_response(response, last)
        retried.update(retried_delta)
        errors.extend(new_errors)

        pending = retry
        if not pending:
            break
        if not last:
            time.sleep(PUT_BACKOFF_SECONDS * 2**attempt)

    # Still outstanding after the last attempt: throttled, never written.
    errors.extend(
        f"UnprocessedEntry: still unwritten after {PUT_RETRIES} retries"
        for _ in pending
    )
    return len(chunk) - len(errors), errors, retried


def chunk_records(records, size):
    """Chunks of `size` records in which no record identifier appears twice.

    A feature group is keyed on (record identifier, event time), so the same
    identifier at many event times is the supported shape — the offline store
    keeps every one and the online store resolves to the latest event time on
    its own. `BatchWriteRecord` only forbids it WITHIN one call: the 25 entries
    have no order among themselves, so the service cannot say which the online
    store should keep, and it rejects the whole call. A monthly panel therefore
    needs its months spread across calls, not collapsed — `deduplicate` has
    already removed the rows that genuinely share a key.

    Occurrences are dealt out round-robin, so every identifier's first event time
    goes out before any second one.
    """
    rounds = defaultdict(list)
    seen = Counter()
    for record in records:
        # The identifier is the first entry build_records appends, always.
        identifier = record[0]["ValueAsString"]
        rounds[seen[identifier]].append(record)
        seen[identifier] += 1
    return [
        batch[start : start + size]
        for _, batch in sorted(rounds.items())
        for start in range(0, len(batch), size)
    ]


def progress_step(total):
    """How many records to write between progress lines, for a run of `total` rows."""
    return max(PROGRESS_EVERY, math.ceil((total or 1) / PROGRESS_REPORTS))


def put_records(runtime, fg_name, records, run_id, done=0, total=None, pool=None):
    """`BatchWriteRecord` in chunks of 25 (AC-3), over `PUT_WORKERS` threads.

    This writes the ONLINE store. Feature Store replicates to the offline store
    asynchronously, so the count returned is not evidence of an offline row.

    `done` and `total` are the whole run's counts, so a slice's progress lines
    report against the run rather than restarting at zero for every slice.

    `pool` is the run's write pool. It is passed in so that a run of hundreds of
    slices spawns PUT_WORKERS threads once rather than per slice; called without
    one, this owns a pool for the single call.
    """
    total = len(records) if total is None else total
    step = progress_step(total)
    chunks = chunk_records(records, PUT_CHUNK_SIZE)
    ingested, errors, reported = 0, [], done
    retried = Counter()
    owned = pool is None
    executor = ThreadPoolExecutor(max_workers=PUT_WORKERS) if owned else pool
    try:
        futures = [executor.submit(_put_chunk, runtime, fg_name, c) for c in chunks]
        for future in as_completed(futures):
            written, chunk_errors, chunk_retried = future.result()
            ingested += written
            errors.extend(chunk_errors)
            retried.update(chunk_retried)
            if done + ingested - reported >= step:
                reported = done + ingested
                _log(
                    "ingest progress",
                    runId=run_id,
                    featureGroup=fg_name,
                    recordsIngested=done + ingested,
                    recordsFailed=len(errors),
                    rowsTotal=total,
                )
    finally:
        if owned:
            executor.shutdown()
    return ingested, errors, retried


def write_report(s3, output_s3_uri, report, account_id, kms_key_arn):
    """The ingest report (AC-6), at the run's processing-output prefix.

    Written with put_object rather than through a ProcessingOutput channel: every
    S3 call in this pipeline carries ExpectedBucketOwner, which a channel upload
    cannot, and the job then needs no input or output channels at all.
    """
    object_name = (
        REPORT_OBJECT_PREFIX + report["ingested_at"].replace(":", "-") + ".json"
    )
    bucket, key = _split_s3_uri(
        output_s3_uri.rstrip("/") + "/" + object_name, "ingest.output_s3_uri"
    )
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=kms_key_arn,
        ExpectedBucketOwner=account_id,
    )
    return f"s3://{bucket}/{key}"


def _resolve_and_apply_dynamic_templates(sm, ingest, df, fg_name, run_id):
    """`ingest`, with every `dynamic_templates` entry resolved against `df`
    and spliced into `ingest["transforms"]` — unchanged if there are none.

    A column-derived Pattern C feature (ingest.dynamic_templates) has no
    concrete name until it is resolved against this same already-fetched
    `df` — no new Snowflake connection, no state-machine reorder. Resolved
    here, once, before compute_features runs, so every declared feature
    (static or resolved-dynamic) goes through the same dispatch path.
    """
    dynamic_templates = ingest.get("dynamic_templates") or []
    if not dynamic_templates:
        return ingest

    reserved = {
        ingest["record_identifier_feature_name"],
        ingest["event_time_feature_name"],
    }
    seen_names = {t["feature"] for t in ingest["transforms"]}
    resolved_transforms, feature_additions, metadata_calls, resolve_errors = (
        resolve_dynamic_templates(dynamic_templates, df, reserved, seen_names, fg_name)
    )
    if resolve_errors:
        raise IngestError(
            "could not resolve dynamic feature template(s): "
            + "; ".join(str(e) for e in resolve_errors)
        )
    feature_group.add_features(sm, fg_name, feature_additions)
    feature_group.apply_metadata(sm, metadata_calls)
    _log(
        "dynamic feature template(s) resolved",
        runId=run_id,
        featureGroup=fg_name,
        templateCount=len(dynamic_templates),
        resolvedFeatureCount=len(resolved_transforms),
    )
    return {**ingest, "transforms": [*ingest["transforms"], *resolved_transforms]}


def _build_and_write_slices(runtime, ingest, df, computed, fg_name, run_id):
    """Build and write every slice of `df`/`computed`, the next slice building
    while this one writes (see this module's docstring for why the two
    overlap).

    Returns `(rejected, write_errors, transient_retries, records_valid,
    ingested, sample_record_id)` — the running totals `main()` folds into the
    ingest report.
    """
    rejected, write_errors = [], []
    transient_retries = Counter()
    records_valid, ingested, sample_record_id = 0, 0, None
    slice_starts = list(range(0, len(df), ROW_SLICE))

    def build_slice(slice_start):
        rows = slice(slice_start, slice_start + ROW_SLICE)
        return build_records(ingest, df.iloc[rows], computed.iloc[rows], slice_start)

    # One builder thread, because building is pure Python and a second would only
    # contend for the same GIL; the writers are what needs the wide pool.
    with (
        ThreadPoolExecutor(max_workers=1) as builder,
        ThreadPoolExecutor(max_workers=PUT_WORKERS) as writers,
    ):
        pending = builder.submit(build_slice, slice_starts[0]) if slice_starts else None
        for position, slice_start in enumerate(slice_starts):
            # `process_time` is CPU summed over EVERY thread, so dividing it by the
            # slice's wall clock gives the number of cores the slice actually kept
            # busy. ~1.0 means the whole slice is one core's worth of serialised
            # Python — the GIL, not the network — and splitting the work across
            # processes is then the only thing that can help. Well under 1.0 means
            # the slice is genuinely waiting and more processes would not.
            cpu_at_slice_start = time.process_time()
            wall_at_slice_start = time.monotonic()
            # Raises here if this slice's build failed — before anything it
            # produced is written
            records, slice_rejected = pending.result()
            # Queued BEFORE the write, which is the whole point: the next slice is
            # built during this slice's network wait.
            pending = (
                builder.submit(build_slice, slice_starts[position + 1])
                if position + 1 < len(slice_starts)
                else None
            )
            rejected.extend(slice_rejected)
            records_valid += len(records)
            if sample_record_id is None and records:
                sample_record_id = records[0][0]["ValueAsString"]

            slice_ingested, slice_errors, slice_retried = put_records(
                runtime,
                fg_name,
                records,
                run_id,
                done=ingested,
                total=len(df),
                pool=writers,
            )
            ingested += slice_ingested
            write_errors.extend(slice_errors)
            transient_retries.update(slice_retried)
            _log(
                "slice written",
                runId=run_id,
                featureGroup=fg_name,
                sliceStart=slice_start,
                recordsValid=len(records),
                rowsRejected=len(slice_rejected),
                recordsIngested=ingested,
                recordsFailed=len(write_errors),
                # Empty on a healthy run. Non-empty means the write pool is wide
                # enough to be throttled, which costs backoff, not correctness.
                sliceRetries=dict(slice_retried),
                sliceWallSeconds=round(time.monotonic() - wall_at_slice_start, 2),
                sliceCpuSeconds=round(time.process_time() - cpu_at_slice_start, 2),
                sliceCoresBusy=round(
                    (time.process_time() - cpu_at_slice_start)
                    / max(time.monotonic() - wall_at_slice_start, 1e-9),
                    2,
                ),
            )
            # Dropped once written, so only this slice and the one being built are
            # held — peak memory is two slices, never three.
            del records

    return (
        rejected,
        write_errors,
        transient_retries,
        records_valid,
        ingested,
        sample_record_id,
    )


def main(argv=None):
    args = _parse_args(argv)
    # ENV catches a document compiled for one environment run in another;
    # ACCOUNT_ID is the ExpectedBucketOwner on every S3 call.
    env_name = config_module.load_var("ENV")
    account_id = config_module.load_var("ACCOUNT_ID")
    region = config_module.load_var("AWS_REGION")
    kms_key_arn = config_module.load_var("KMS_KEY_ARN")
    settings = snowflake_settings()

    s3 = boto3.client("s3", region_name=region)
    secrets = boto3.client("secretsmanager", region_name=region)
    # Sized to the write pool: this is the client PUT_WORKERS threads share for
    # every BatchWriteRecord, and at botocore's default of 10 connections the
    # other 40 are opened, used once and discarded, paying a TLS handshake a call.
    #
    # parameter_validation=False because botocore's validator walks every member of
    # every entry on every call — profiled as the single largest cost in the write
    # path, ahead of serialization, and it holds the GIL the builder needs. The
    # records are machine-built by `build_records` against the compiled intake, not
    # hand-written, so the trade (a malformed param surfaces as a service
    # ValidationError rather than client-side) buys a measured 1.92 -> 1.10 ms/call.
    runtime = boto3.client(
        FEATURE_STORE_RUNTIME,
        region_name=region,
        config=Config(max_pool_connections=PUT_WORKERS, parameter_validation=False),
    )
    sm = boto3.client("sagemaker", region_name=region)

    intake = compiled_intake.parse(
        _read_s3_text(
            s3, args.compiled_intake_s3_uri, "compiled_intake_s3_uri", account_id
        )
    )
    fg_name = intake["feature_group_name"]
    run_id = args.run_id

    # The run id is part of the object key, so a mismatch means this run was
    # handed another run's document.
    if intake["run_id"] != run_id:
        raise IngestError(
            f"run_id: argument {run_id!r} does not match the compiled intake's "
            f"{intake['run_id']!r}."
        )
    if intake["env"] != env_name:
        raise IngestError(
            f"env: the compiled intake was compiled for {intake['env']!r} but this "
            f"job runs in {env_name!r}."
        )

    ingest = intake["ingest"]
    # parse() guarantees the top-level keys only; a reader checks the keys it
    # reads. A document written before 2026-09-16 carries the SQL inline instead.
    if "source_sql_s3_uri" not in ingest:
        raise IngestError(
            "ingest.source_sql_s3_uri is missing from the compiled intake. A "
            "document written before 2026-09-16 carries the clipped SQL inline as "
            "ingest.source_sql; re-run stage 1 to get the current layout."
        )

    source_sql = _read_s3_text(
        s3, ingest["source_sql_s3_uri"], "ingest.source_sql_s3_uri", account_id
    )

    warehouse = snowflake_warehouse(settings, ingest)
    _log(
        "reading Gold with the clipped source query",
        runId=run_id,
        featureGroup=fg_name,
        sourceSql=ingest["source_sql_s3_uri"],
        asOf=intake["as_of"],
        snowflakeAccount=settings["SNOWFLAKE_ACCOUNT"],
        # Logged because the warehouse decides what the read costs and how fast it is.
        snowflakeWarehouse=warehouse,
    )

    connection = connect_to_snowflake(secrets, settings, warehouse)
    try:
        df = read_source(connection, source_sql)
    finally:
        connection.close()

    validate_identity_columns(list(df.columns), ingest)
    absent = missing_columns(list(df.columns), ingest)
    if absent:
        raise IngestError(
            f"source columns declared in the form but not returned by the query: "
            f"{absent}. Returned: {list(df.columns)}."
        )

    ingest = _resolve_and_apply_dynamic_templates(sm, ingest, df, fg_name, run_id)

    rows_read = len(df)
    # Aggregating pivot transforms (counts/flags over a record's many event rows)
    # must see EVERY row, so their wide results are computed over the full frame
    # here, BEFORE de-duplication. Each wide result is one row per entity, so this
    # does not materialise anything at the full-frame row count.
    wide_cache = _wide_results(df, ingest)
    df, duplicates = deduplicate(df, ingest)
    if duplicates:
        _log(
            "duplicate keys collapsed before the write",
            runId=run_id,
            featureGroup=fg_name,
            rowsRead=rows_read,
            rowsDuplicate=duplicates,
        )
    # `computed` is built at the deduped record grain: pivot features are joined
    # from `wide_cache`, every other feature is computed on the deduped frame
    # exactly as before.
    computed, lineage = compute_features(df, ingest, wide_cache)
    unproduced = missing_features(computed, ingest)
    if unproduced:
        raise IngestError(
            f"features declared in the form but not produced by any transformation: "
            f"{unproduced}. Produced: {list(computed.columns)}."
        )
    _log(
        "features computed",
        runId=run_id,
        featureGroup=fg_name,
        featureCount=len(lineage),
        transforms=lineage,
    )

    (
        rejected,
        write_errors,
        transient_retries,
        records_valid,
        ingested,
        sample_record_id,
    ) = _build_and_write_slices(runtime, ingest, df, computed, fg_name, run_id)

    ingested_at = utc_now()
    report = {
        "feature_group": fg_name,
        "run_id": run_id,
        "as_of": intake["as_of"],
        "rows_read": rows_read,
        "rows_duplicate": duplicates,
        "rows_ingested": ingested,
        "rows_rejected": len(rejected),
        "rejected": rejected,
        "rows_failed": len(write_errors),
        "write_errors": write_errors[:WRITE_ERROR_SAMPLE],
        # Transient failures the writes retried through successfully. Zero on a
        # healthy run; non-zero is the signal that PUT_WORKERS is wide enough to be
        # throttled, which shows up as backoff rather than as a failure.
        "transient_retries": dict(transient_retries),
        # The transform map AC-6 names: each feature and the module.function applied.
        "transforms": lineage,
        "ingested_at": ingested_at,
        # A real ingested identifier, so the online-store check has one to ask for.
        "sample_record_id": sample_record_id,
    }
    report_uri = write_report(
        s3, ingest["output_s3_uri"], report, account_id, kms_key_arn
    )

    _log(
        "ingest complete",
        runId=run_id,
        featureGroup=fg_name,
        rowsRead=rows_read,
        rowsDuplicate=duplicates,
        rowsIngested=ingested,
        rowsRejected=len(rejected),
        rowsFailed=len(write_errors),
        transientRetries=dict(transient_retries),
        ingestReport=report_uri,
    )
    if write_errors:
        raise IngestError(
            f"{len(write_errors)} record(s) failed PutRecord after {PUT_RETRIES} "
            f"retries; {ingested} of {records_valid} landed. See {report_uri}. "
            f"First: {write_errors[0]}"
        )
    return report


if __name__ == "__main__":
    main()
