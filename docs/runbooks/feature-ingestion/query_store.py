#!/usr/bin/env python3
"""Read a feature group back, from either store, after the pipeline has run.

The two stores answer different questions, so this does both:

  ONLINE  (--online --id <id>)  the low-latency point lookup M7 serving uses:
                                one record, latest values, single-digit ms.
  OFFLINE (--offline)           the bulk historical read M5 training uses:
                                append-only Parquet, every version ever written.

Which feature group: taken from $INTAKE_DIR's form, with the registered name
composed the same way the compiler composes it (ar-<wl>-<env>-fgr-<name>), so this
cannot drift from what the pipeline created. `--feature-group` names a registered
group directly instead, for a group you have no intake directory for.

Run:
    export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/...
    query_store.py --online --id P00008
    query_store.py --offline
    query_store.py --offline --asof 2026-01-05T00:00:00Z     # point-in-time cutoff
    query_store.py --offline --athena                        # SQL instead of Parquet

TWO OFFLINE READ PATHS, and the default is deliberate. `--athena` runs real SQL
against the Glue table, which is what M5 does — but the table is Lake
Formation-governed, so it needs a Describe grant your role may not have. The
default reads the Parquet straight out of S3 with pandas, which needs only S3 read
and therefore works without that grant. Same numbers, no catalog in the way.

Exit codes: 0 read something, 1 the read failed, 2 nothing matched.
"""

import argparse
import io
import json
import os
import pathlib
import sys
import time

import boto3
import pandas as pd
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.common import intake_compiler  # noqa: E402  (after path insert)
from src.common.config import DEFAULT_WORKLOAD  # noqa: E402

# Feature Store appends these to every offline row; they are not features.
HOUSEKEEPING = ("write_time", "api_invocation_time", "is_deleted")
SAMPLE_ROWS = 5

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_EMPTY = 2


class _Naming:
    """The two fields intake_compiler.feature_group_name() reads off a config."""

    def __init__(self, env):
        self.env = env
        self.workload = os.environ.get("WORKLOAD") or DEFAULT_WORKLOAD


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Query a feature group's online or offline store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--online", action="store_true", help="GetRecord point lookup")
    parser.add_argument("--offline", action="store_true", help="bulk historical read")
    parser.add_argument("--id", help="record identifier to fetch (with --online)")
    parser.add_argument(
        "--features", default="", help="comma-separated subset to fetch (--online)"
    )
    parser.add_argument(
        "--asof", help="offline point-in-time cutoff: event_time <= this"
    )
    parser.add_argument(
        "--athena",
        action="store_true",
        help="read offline via Athena SQL instead of Parquet (needs a Lake "
        "Formation grant on the table)",
    )
    parser.add_argument(
        "--feature-group", help="registered feature group name, instead of $INTAKE_DIR"
    )
    parser.add_argument("--intake-dir", type=pathlib.Path, help="default: $INTAKE_DIR")
    parser.add_argument("--env", default=os.environ.get("ENV", "dev"))
    return parser.parse_args(argv)


def registered_name(args):
    """The feature group name as REGISTERED, not as the form spells it."""
    if args.feature_group:
        return args.feature_group

    given = args.intake_dir or os.environ.get("INTAKE_DIR", "").strip()
    if not given:
        raise SystemExit(
            "[fail] no feature group. Export INTAKE_DIR (see the runbook), or pass "
            "--feature-group <registered name>."
        )
    intake_dir = pathlib.Path(given).expanduser().resolve()
    forms = sorted(p for p in intake_dir.glob("*.y*ml") if p.is_file())
    if len(forms) != 1:
        raise SystemExit(
            f"[fail] expected one intake form in {intake_dir}, found {len(forms)}."
        )
    form = yaml.safe_load(forms[0].read_text()) or {}
    declared = ((form.get("identity") or {}).get("feature_group_name") or "").strip()
    if not declared:
        raise SystemExit(f"[fail] {forms[0].name} has no identity.feature_group_name.")
    return intake_compiler.feature_group_name(_Naming(args.env), declared)


def describe(sagemaker, name):
    """The control plane is the only place that knows the schema and the store."""
    try:
        return sagemaker.describe_feature_group(FeatureGroupName=name)
    except sagemaker.exceptions.ResourceNotFound as exc:
        raise SystemExit(
            f"[fail] no feature group {name!r} in this account and Region — has the "
            f"pipeline run? ({exc})"
        ) from exc


def cast(value, feature_type):
    """ValueAsString back to a Python type, using the declared FeatureType.

    The wire is strings in both directions; nothing casts for us, so an Integral
    feature read back without this is the string "4", not 4.
    """
    if value is None or value == "":
        return None
    if feature_type == "Fractional":
        return float(value)
    if feature_type == "Integral":
        return int(float(value))
    return value


def read_online(session, description, args):
    if not args.id:
        raise SystemExit("[fail] --online needs --id <record identifier>.")

    name = description["FeatureGroupName"]
    request = {"FeatureGroupName": name, "RecordIdentifierValueAsString": args.id}
    if args.features.strip():
        request["FeatureNames"] = [f.strip() for f in args.features.split(",")]

    response = session.client("sagemaker-featurestore-runtime").get_record(**request)

    # A miss is NOT an error: the response simply carries no Record. Serving code
    # has to treat "no features for this id" as an expected case, so this does too.
    if "Record" not in response:
        print(
            f"[empty] no online record for "
            f"{description['RecordIdentifierFeatureName']}={args.id!r}.\n"
            f"        The online store is written by PutRecord during ingest and "
            f"expires on the group's TTL, so an id that IS in the offline store can "
            f"still be absent here."
        )
        return EXIT_EMPTY

    types = {
        d["FeatureName"]: d["FeatureType"] for d in description["FeatureDefinitions"]
    }
    raw = {f["FeatureName"]: f.get("ValueAsString") for f in response["Record"]}
    parsed = {n: cast(v, types.get(n, "String")) for n, v in raw.items()}

    print(f"[ok] {len(raw)} features for {args.id!r}\n")
    print(json.dumps(parsed, indent=2))
    return EXIT_OK


def _offline_frame(session, description):
    """Every offline Parquet object for this group, concatenated."""
    uri = description["OfflineStoreConfig"]["S3StorageConfig"]["ResolvedOutputS3Uri"]
    bucket, _, prefix = uri[len("s3://") :].partition("/")

    s3 = session.client("s3")
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        keys += [
            o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".parquet")
        ]
    if not keys:
        print(
            f"[empty] no Parquet under {uri}.\n"
            f"        Feature Store flushes the offline store asynchronously — "
            f"minutes after ingest, not at PutRecord. Try again shortly."
        )
        return None, uri

    frames = []
    for key in keys:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        frames.append(pd.read_parquet(io.BytesIO(body)))
    return pd.concat(frames, ignore_index=True), uri


def _latest_per_id(frame, id_column, event_time):
    """One row per id: newest event_time, breaking ties on write_time.

    The offline store is APPEND-ONLY — re-running ingest leaves the old rows in
    place — so "current value" is a query-time decision here, not a stored fact.
    Tombstones (is_deleted) are dropped rather than ranked.
    """
    live = frame
    if "is_deleted" in live.columns:
        live = live[~live["is_deleted"].fillna(False).astype(bool)]
    order = [c for c in (event_time, "write_time") if c in live.columns]
    return (
        live.sort_values(order, ascending=False)
        .drop_duplicates(subset=[id_column], keep="first")
        .reset_index(drop=True)
    )


def read_offline_parquet(session, description, args):
    id_column = description["RecordIdentifierFeatureName"]
    event_time = description["EventTimeFeatureName"]

    frame, uri = _offline_frame(session, description)
    if frame is None:
        return EXIT_EMPTY
    print(f"[source] {uri}")

    if args.asof:
        before = len(frame)
        frame = frame[frame[event_time] <= args.asof]
        print(
            f"[asof]   {event_time} <= {args.asof}: kept {len(frame)} of "
            f"{before} row(s) — the rest is what a model trained 'as of' that "
            f"instant must not see."
        )
        if frame.empty:
            print("[empty] nothing at or before that cutoff.")
            return EXIT_EMPTY

    latest = _latest_per_id(frame, id_column, event_time)
    print(
        f"[rows]   {len(frame)} raw / {frame[id_column].nunique()} distinct "
        f"{id_column} / {len(latest)} after latest-per-id"
    )
    if len(frame) != len(latest):
        print(
            "[note]   raw exceeds latest-per-id: the store is append-only, so "
            "re-runs accumulate versions. Deduplicate on read."
        )

    features = [c for c in frame.columns if c not in HOUSEKEEPING]
    print(f"\n[latest per {id_column}, first {SAMPLE_ROWS}]")
    print(latest[features].head(SAMPLE_ROWS).to_string(index=False))
    return EXIT_OK


def read_offline_athena(session, description, args):
    catalog = description["OfflineStoreConfig"].get("DataCatalogConfig")
    if not catalog:
        print(
            "[fail] this feature group has no Glue table, so there is nothing for "
            "Athena to query — it was created with DisableGlueTableCreation. Drop "
            "--athena to read the Parquet from S3 instead.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    database, table = catalog["Database"], catalog["TableName"]
    id_column = description["RecordIdentifierFeatureName"]
    event_time = description["EventTimeFeatureName"]

    region = session.region_name
    account = session.client("sts").get_caller_identity()["Account"]
    output = os.environ.get(
        "ATHENA_OUTPUT_S3_URI",
        f"s3://sagemaker-{region}-{account}/novos-ar-athena-results/",
    )
    print(f"[catalog] {database}.{table}")
    print(f"[results] {output}")

    where = f"WHERE {event_time} <= '{args.asof}' " if args.asof else ""
    sql = (
        f"WITH ranked AS (SELECT *, ROW_NUMBER() OVER ("
        f"PARTITION BY {id_column} ORDER BY {event_time} DESC, write_time DESC"
        f') AS rn FROM "{database}"."{table}" {where}) '
        f"SELECT * FROM ranked WHERE rn = 1 AND NOT is_deleted "
        f"ORDER BY {id_column} LIMIT {SAMPLE_ROWS}"
    )
    print(f"[sql]     {sql}\n")

    athena = session.client("athena")
    started = athena.start_query_execution(
        QueryString=sql, ResultConfiguration={"OutputLocation": output}
    )["QueryExecutionId"]

    # Athena is async, the same way the offline flush is.
    while True:
        status = athena.get_query_execution(QueryExecutionId=started)["QueryExecution"][
            "Status"
        ]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)

    if status["State"] != "SUCCEEDED":
        reason = status.get("StateChangeReason", "")
        print(f"[fail] Athena {status['State']}: {reason}", file=sys.stderr)
        if "Lake Formation" in reason:
            print(
                "[hint] the Glue table is Lake Formation-governed and your role has "
                "no grant on it. Drop --athena to read the Parquet from S3 instead, "
                "which needs only S3 read.",
                file=sys.stderr,
            )
        elif "Access denied when writing" in reason:
            print(
                "[hint] set ATHENA_OUTPUT_S3_URI to a prefix you can write.",
                file=sys.stderr,
            )
        return EXIT_FAILED

    rows = athena.get_query_results(QueryExecutionId=started)["ResultSet"]["Rows"]
    if len(rows) < 2:
        print("[empty] the query matched no rows.")
        return EXIT_EMPTY
    header = [c.get("VarCharValue") for c in rows[0]["Data"]]
    print("[cols] " + ", ".join(h for h in header if h not in ("rn",)))
    for row in rows[1:]:
        print("       " + str([c.get("VarCharValue") for c in row["Data"]]))
    return EXIT_OK


def main(argv=None):
    args = _parse_args(argv)
    if not (args.online or args.offline):
        raise SystemExit("[fail] pass --online (with --id) or --offline.")
    # Checked before the first AWS call, so a missing --id is not reported as
    # whatever DescribeFeatureGroup happens to say.
    if args.online and not args.id:
        raise SystemExit("[fail] --online needs --id <record identifier>.")

    session = boto3.Session()
    name = registered_name(args)
    description = describe(session.client("sagemaker"), name)
    print(f"[group]  {name} ({description['FeatureGroupStatus']})")

    result = EXIT_OK
    if args.online:
        result = read_online(session, description, args) or result
    if args.offline:
        reader = read_offline_athena if args.athena else read_offline_parquet
        result = reader(session, description, args) or result
    return result


if __name__ == "__main__":
    sys.exit(main())
