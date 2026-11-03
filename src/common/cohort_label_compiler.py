"""Compile a validated cohort-label intake form into the compiled intake.

The compiled intake is the contract between the two stages of the cohort-label
state machine. It is written once, here, and read by the population + label
Processing job, which never re-parses the YAML form — nothing downstream re-derives
what the validator decided (SPEC-M4 §2.3). It carries:

    population    the rendered source query by reference, the strategy dispatch
                  block, the criteria, and the keyword arguments the pipeline (not
                  the form) supplies to the strategy
    label         the transformation dispatch block with `params` bridged to real
                  engine keyword arguments, the single output column, and the
                  keyword arguments the pipeline supplies
    storage       the destinations the form asked for, platform-composed
    compute       the image, instance type and count the job runs on
    job           the Processing job name, length-asserted here rather than at
                  submission time

A source query is written as its own object rather than inlined: a query has no
useful size bound. A `.sql` source's `${...}` placeholders are substituted once,
here, so the job receives final SQL and never needs the form's `params` again. A
`.py` (Snowpark) source is written UNRENDERED — `population.source_params`
carries the form's `params` dict instead, since a `SnowparkSource` subclass reads
its own configuration as a dict argument, not a textual substitution — see
`population.source_kind`.

No Snowflake connection detail appears anywhere in the document — not even a secret
ARN. The Processing job receives account, host, warehouse, role and the two secret
ids as environment variables from `snowflake.env_vars` and resolves the credential
itself, over PrivateLink on TCP 443.
"""

import os
import re
from datetime import datetime, timezone

from src.common.cohort_label_schema import (
    is_snowpark_source,
    label_engine_block,
    label_feature_groups,
    local_modules,
    query_block,
    render_query,
)

# The prefix a registered feature group name always carries. Mirrors
# `mrd_compiler.registered_feature_group_name`: a form may already name the
# full registered group (this pipeline reads a group M2 created, never
# creates one itself), so a name already carrying the prefix is used as-is.
_FEATURE_GROUP_PREFIX = "ar-{workload}-{env}-fgr-"

COMPILED_INTAKE_OBJECT = "compiled_intake.json"
POPULATION_SOURCE_OBJECT = "population_source.sql"
LABEL_SOURCE_OBJECT = "label_source.sql"
POPULATION_SOURCE_PY_OBJECT = "population_source.py"
LABEL_SOURCE_PY_OBJECT = "label_source.py"

# Everything stage 1 writes goes under this prefix, so a run's compile outputs sit
# apart from the population + label job's own output.
COMPILE_OUTPUT_PREFIX = "compile-output"
POPULATION_LABEL_PREFIX = "population-label"

# The versioned destination `storage.s3` asks for, which the platform names: the
# form declares only whether to write and an optional prefix. Distinct from
# [Run ID]/population-label/, which is scratch.
COHORT_DATASET_PREFIX = "cohort-datasets"

JOB_KIND = "cl"
# V26 — ProcessingJobName is capped here and must be unique per account and Region.
PROCESSING_JOB_NAME_MAX_LEN = 63

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class CompileError(Exception):
    """The form is valid but cannot be compiled in this environment."""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compile_output_prefix(run_id):
    """[Run ID]/compile-output"""
    return f"{run_id}/{COMPILE_OUTPUT_PREFIX}"


def compiled_intake_key(run_id):
    """[Run ID]/compile-output/compiled_intake.json"""
    return f"{compile_output_prefix(run_id)}/{COMPILED_INTAKE_OBJECT}"


def source_key(run_id, object_name):
    """[Run ID]/compile-output/<object_name>"""
    return f"{compile_output_prefix(run_id)}/{object_name}"


def output_prefix(run_id):
    """[Run ID]/population-label/ — the labelled cohort and the cohort-label report."""
    return f"{run_id}/{POPULATION_LABEL_PREFIX}/"


def slug(text):
    """A name segment safe for a job name: lowercase, hyphen-separated."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def processing_job_name(model_name, run_id):
    """cl-<model>-<runid>, asserted against the 63-character cap.

    Asserted rather than trusted (V26): a name built too long fails mid-pipeline,
    after the orchestration has already committed to the run.
    """
    name = f"{JOB_KIND}-{slug(model_name)}-{run_id}"
    if len(name) > PROCESSING_JOB_NAME_MAX_LEN:
        raise CompileError(
            f"composed processing job name {name!r} is {len(name)} characters; "
            f"SageMaker accepts at most {PROCESSING_JOB_NAME_MAX_LEN}. Shorten "
            f"identity.model_name."
        )
    return name


def cohort_dataset_s3_uri(config, form, compiled_at):
    """The versioned S3 destination `storage.s3` asks for, named by the platform.

    Timestamp-versioned and never overwritten: content-hashed immutable versions are
    deferred (SPEC-M4 §7), so the compile timestamp is the version. `storage.s3` has
    no `enabled` flag in the canon template — the cohort dataset is always written
    to S3, with `s3_prefix` an optional naming refinement.
    """
    s3 = (form.get("storage") or {}).get("s3") or {}
    name = slug((form.get("identity") or {}).get("model_name"))
    prefix = slug(s3.get("s3_prefix") or "")
    if prefix:
        name = f"{name}-{prefix}"
    version = compiled_at.replace(":", "-")
    return (
        f"s3://{config.cohort_label_bucket}/{COHORT_DATASET_PREFIX}/{name}/{version}/"
    )


def snowflake_destination(form):
    """The `storage.snowflake` target, or None.

    `warehouse_size` travels as the SIZE the form declared, not a warehouse name:
    the job composes the name through SNOWFLAKE_WAREHOUSE_PATTERN, which is
    per-environment configuration and does not belong in a compiled artifact.
    """
    snowflake = (form.get("storage") or {}).get("snowflake") or {}
    if not snowflake.get("enabled"):
        return None
    return {
        "account": snowflake.get("account"),
        "database": snowflake.get("database"),
        "schema": snowflake.get("schema"),
        "table": snowflake.get("table"),
        "write_mode": snowflake.get("write_mode") or "append",
        "warehouse_size": snowflake.get("warehouse_size") or "S",
    }


def outcome_name(form):
    """The single label column the label strategy writes.

    `label.output_target_columns` is a list in the template and the strategies
    take a scalar. Validation refuses anything but exactly one entry, so by here
    the unwrap is safe.
    """
    targets = (form.get("label") or {}).get("output_target_columns") or []
    if isinstance(targets, str):
        targets = [targets]
    if not targets:
        raise ValueError("label.target_variable is required but is missing or empty")
    return targets[0]


def label_engine_kwargs(form):
    """The keyword arguments the PIPELINE supplies to the label strategy.

    Only `outcome_name` — the label column the strategy writes, named once in
    `label.output_target_columns` rather than duplicated here. Every other
    keyword argument (including the four maturity ones) lives inside
    `label.transformation.params`, bridged by `label_engine_block` from the
    template's nested `maturity` dict.
    """
    return {"outcome_name": outcome_name(form)}


def registered_feature_group_name(config, declared):
    """The name DescribeFeatureGroup answers to, from what the form declares.

    Mirrors `mrd_compiler.registered_feature_group_name`: this pipeline only
    ever READS a feature group M2 created, so a form may name either the
    short source name or the already-registered `ar-<wl>-<env>-fgr-<name>`
    form directly (M2's own runbook documents both). A name already carrying
    the prefix is used as-is rather than double-prefixed.
    """
    declared = str(declared or "")
    prefix = _FEATURE_GROUP_PREFIX.format(workload=config.workload, env=config.env)
    if declared.startswith(prefix):
        return declared
    return f"{prefix}{declared.replace('_', '-')}"


def resolved_label_feature_groups(config, form):
    """`label.source.feature_groups`, normalized so the job needs no defaulting.

    Each entry's `transformation` writes `label.output_target_columns[0]` —
    validation (`_label_feature_groups_checks`) already refused more than one
    entry when that is the case, so there is exactly one destination column
    per compiled document, the same as `label.transformation` itself.
    """
    return [
        {
            "feature_group_name": registered_feature_group_name(
                config, group.get("feature_group_name")
            ),
            "record_id": group.get("record_id"),
            "event_time": group.get("event_time"),
            "feature_columns": list(group.get("feature_columns") or []),
            "transformation": group.get("transformation") or {},
        }
        for group in label_feature_groups(form)
        if isinstance(group, dict)
    ]


def local_transformation_modules(form):
    """Every form-local dispatch module, with the path it must be staged under.

    The container imports these by the module path the form DECLARES, so the code
    channel has to carry them at exactly that path.
    """
    return [
        {"module": module, "relative_path": os.path.join(*module.split(".")) + ".py"}
        for module in local_modules(form)
    ]


def processing_image_uri(config, form, identity):
    """The cohort-label image built for THIS model, named by the form's model_name."""
    image_tag = str(identity.get("image_tag") or "").strip()
    if image_tag:
        return config.ecr_image_uri(config.ecr_repo_name, image_tag)
    model_name = str((form.get("identity") or {}).get("model_name") or "").strip()
    if not model_name:
        raise CompileError(
            "identity.model_name is missing or empty, so the cohort-label image cannot "
            "be named. It is what build_and_push.sh tagged the image with, so a run "
            "cannot fall back to another model's image — fill it in, or pass image_tag "
            "to pin the image."
        )
    return config.model_processing_image_uri(model_name)


def run_tags(config, form, run_id):
    """The six mandatory Novartis cost-allocation tags, plus ar:runid and ar:owner."""
    owners = (form.get("identity") or {}).get("owners") or []
    tags = dict(config.mandatory_tags)
    tags["ar:runid"] = run_id
    tags["ar:owner"] = " ".join(str(owner) for owner in owners)
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def _rendered_query(intake_dir, node, field):
    """(relative path, final SQL, params) for one source_query block, or
    (None, None, None).

    A `.sql` path is TEXT, `${...}`-rendered here so the job never needs
    `params` again. A `.py` (Snowpark) path is returned UNRENDERED: its
    `params` reach `get_dataframe(session, params)` as a dict argument, the
    same way `${var}` in a `.sql` file reaches the query as a substituted
    literal — each source kind takes configuration the way that kind
    already knows how to.
    """
    block = query_block(node)
    if not block["path"]:
        return None, None, None
    with open(os.path.join(intake_dir, block["path"])) as handle:
        text = handle.read()
    if is_snowpark_source(block["path"]):
        return block["path"], text, block["params"]
    return block["path"], render_query(text, block["params"], field), None


def _resolve_population_source(intake_dir, population, run_id, run_prefix, objects):
    """Render the population source query and stage it under `objects`.

    Returns `(population_path, population_sql_uri, population_is_snowpark,
    population_params)`.
    """
    population_path, population_source, population_params = _rendered_query(
        intake_dir,
        (population.get("source") or {}).get("source_query"),
        "population.source.source_query",
    )
    population_is_snowpark = is_snowpark_source(population_path)
    population_object = (
        POPULATION_SOURCE_PY_OBJECT
        if population_is_snowpark
        else POPULATION_SOURCE_OBJECT
    )
    objects[source_key(run_id, population_object)] = population_source
    population_sql_uri = f"{run_prefix}/{COMPILE_OUTPUT_PREFIX}/{population_object}"
    return (
        population_path,
        population_sql_uri,
        population_is_snowpark,
        population_params,
    )


def _resolve_label_source(
    intake_dir, label, run_id, run_prefix, population_path, population_sql_uri, objects
):
    """Render the label source query (if any) and stage it under `objects`.

    Returns `label_sql_uri`: `None` when the label is computed from the
    population query's own columns, the population's own URI when one query
    serves both (writing it twice would let the copies drift), or a freshly
    staged label-source URI otherwise.
    """
    label_source_node = ((label.get("source") or {}).get("snowflake") or {}).get(
        "source_query"
    )
    label_path, label_source, _ = _rendered_query(
        intake_dir, label_source_node, "label.source.snowflake.source_query"
    )
    if label_path is None:
        return None
    if label_path == population_path:
        return population_sql_uri
    label_object = (
        LABEL_SOURCE_PY_OBJECT
        if is_snowpark_source(label_path)
        else LABEL_SOURCE_OBJECT
    )
    objects[source_key(run_id, label_object)] = label_source
    return f"{run_prefix}/{COMPILE_OUTPUT_PREFIX}/{label_object}"


def compile_intake(config, form, intake_dir, identity, run_id, as_of=None):
    """Compile a validated form.

    Returns `(compiled_intake, objects)`, where `objects` maps an object key under
    [Run ID]/compile-output/ to the text to write there — the rendered source
    queries. The caller writes them, so this function stays free of AWS.

    `identity` carries what the execution input named: model_id, the optional
    feature_group_name, the optional image_tag, and the intake directory the form was
    read from. Not to be confused with the form's own `identity` section
    (business_unit/model_name/...).
    """
    as_of = as_of or utc_now()
    if not _AS_OF_RE.match(str(as_of)):
        raise CompileError(
            f"as_of {as_of!r} must be an ISO-8601 UTC instant, e.g. "
            f"2026-09-15T00:00:00Z."
        )
    compiled_at = utc_now()

    population = form.get("population") or {}
    label = form.get("label") or {}
    compute = form.get("compute") or {}
    model_name = (form.get("identity") or {}).get("model_name")
    run_prefix = f"s3://{config.cohort_label_bucket}/{run_id}"

    objects = {}
    (
        population_path,
        population_sql_uri,
        population_is_snowpark,
        population_params,
    ) = _resolve_population_source(intake_dir, population, run_id, run_prefix, objects)

    label_sql_uri = _resolve_label_source(
        intake_dir,
        label,
        run_id,
        run_prefix,
        population_path,
        population_sql_uri,
        objects,
    )

    target_type = label.get("target_type")
    compiled = {
        "run_id": run_id,
        "compiled_at": compiled_at,
        "as_of": as_of,
        "env": config.env,
        "model_name": model_name,
        "model_id": identity.get("model_id"),
        # Carried forward for linkage only — the form is located by model alone.
        "feature_group_name": identity.get("feature_group_name"),
        "intake_s3_uri": identity.get("intake_s3_uri"),
        "population": {
            "entity_type": population.get("entity_type"),
            "entity_id_column": population.get("entity_id_column"),
            "inclusion_criteria": list(population.get("inclusion_criteria") or []),
            "exclusion_criteria": list(population.get("exclusion_criteria") or []),
            # By reference, not inline — see this module's docstring.
            "source_sql_s3_uri": population_sql_uri,
            # "sql" (the object is final, ${...}-rendered text to execute
            # verbatim) or "snowpark" (the object is a .py module implementing
            # ar_transformations.snowpark.base.SnowparkSource; source_params is
            # its get_dataframe(session, params) argument, unrendered, since a
            # Snowpark source reads its own configuration rather than taking a
            # textual substitution).
            "source_kind": "snowpark" if population_is_snowpark else "sql",
            "source_params": population_params if population_is_snowpark else None,
            "strategy": population.get("strategy"),
            # `reference_date` is pinned to as_of so a rolling anchor resolves the
            # same way on a rerun instead of drifting with the wall clock.
            # `entity_id_column` is NOT repeated here: canon has the YAML author
            # repeat `population.entity_id_column` directly into
            # `strategy.params.entity_id_column` (the dispatcher does no
            # special-casing to merge it in) -- supplying it a second time here
            # would collide with the form's own copy as a duplicate keyword
            # argument at call time.
            "engine_kwargs": {
                "reference_date": as_of,
            },
        },
        "label": {
            "target_type": target_type,
            "label_column": outcome_name(form),
            # Carried forward alongside `label_column` (unchanged, still what this
            # pipeline's own job reads) so a downstream consumer with more than one
            # written label column -- and `mrd_upstream.py`, which already reads this
            # exact key -- has the full list rather than the single-column unwrap.
            "output_target_columns": list(label.get("output_target_columns") or []),
            "input_target_columns": list(label.get("input_target_columns") or []),
            "source_sql_s3_uri": label_sql_uri,
            # transformation carries `params` already bridged to real engine
            # keyword arguments (maturity flattened, output_target_columns
            # dropped) — the Processing job dispatches it exactly as declared
            # here, with no re-derivation of the template's own nesting.
            "transformation": label_engine_block(form),
            "engine_kwargs": label_engine_kwargs(form),
            # Joined onto the population frame BEFORE label.transformation runs
            # (or, when that is blank, before the passthrough rename) — see
            # `population_label.apply_label_feature_groups`. Empty when the
            # label is computed entirely from population-source columns.
            "feature_groups": resolved_label_feature_groups(config, form),
        },
        "local_transformations": local_transformation_modules(form),
        "storage": {
            "s3_uri": cohort_dataset_s3_uri(config, form, compiled_at),
            "snowflake": snowflake_destination(form),
        },
        "compute": {
            "image_uri": processing_image_uri(config, form, identity),
            "instance_type": compute["instance_type"],
            "instance_count": int(compute.get("instance_count", 1)),
        },
        "job": {
            "processing_job_name": processing_job_name(model_name, run_id),
            "role_arn": config.cohort_label_execution_role_arn,
            "output_s3_uri": f"{run_prefix}/{POPULATION_LABEL_PREFIX}/",
            "tags": run_tags(config, form, run_id),
        },
    }
    return compiled, objects
