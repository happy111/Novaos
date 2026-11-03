"""cohort-label-intake-form.yaml schema and validation (AR-18-a-2 AC-3, AC-4).

Realigned to the canonical template on `feature/NTU-833-836-837` in the AR repo —
the ONLY source of truth for this form's shape. Every check here runs on the
parsed form plus the intake directory the DS staged, so a malformed form is
rejected BEFORE the Processing job and the Snowflake read. The only AWS calls
ahead of validation are the S3 reads of the intake directory itself, which is
what supplies the input.

Two phases, deliberately:

  1. `_structural_checks` — the raw parsed form alone: required sections, the
     hard gates, and the cross-parameter windowing logic a function signature
     cannot express.
  2. `_resolved_checks` — needs the staged directory: the source queries exist
     and every `${...}` binds, and every dispatch block names a real callable
     whose keyword arguments the form actually supplies.

Phase 2 runs only when phase 1 is clean, so one missing key produces one finding
rather than a cascade of its consequences.

Two severities. ERROR will actually fail downstream and blocks the run; WARN will
not crash anything by itself but wants a human's eyes first — `reviewed_by_lead_ds`
being false is the canonical example, a Dev run's warning and a Prod run's blocker.

There is no registry of strategies. A form names a module and a function, and
adding a strategy is writing one, so nothing here knows how many population
strategies exist (AR-18-a-2's dispatch note). `label.target_type` is the one
closed enumeration: it no longer selects a family block (the canon template
collapsed that discriminated union to a single `label.transformation` block), it
only names which `ar_transformations.labels` function `transformation.function`
is expected to match.
"""

import os
import re

ERROR = "ERROR"
WARN = "WARN"

REQUIRED_SECTIONS = ["identity", "population", "label", "storage", "compute"]

FORM_FILENAME = "cohort-label-intake-form.yaml"

LABEL_FAMILIES = (
    "binary",
    "regression",
    "multiclass",
    "multilabel",
    "ranking",
    "time_to_event",
)

ALLOWED_ANCHOR_TYPES = {"fixed", "per_entity_event", "rolling", "expression"}
ALLOWED_WINDOW_TYPES = {"static", "dynamic"}
ALLOWED_IMMATURE_POLICY = {"exclude", "censor"}
ALLOWED_WRITE_MODES = {"append", "overwrite"}
ALLOWED_WAREHOUSE_SIZES = {"S", "M", "L"}
ALLOWED_LABEL_SOURCE_LOCATIONS = {"none", "snowflake", "feature_groups"}

# Dispatch module roots that resolve by IMPORT, from the pinned transformations
# distribution. Anything else is a form-local tier resolved by path, beside the
# form. Only these directories are staged into the Processing image, so a module
# named elsewhere resolves on the author's machine but not in a real run.
PACKAGED_TIERS = ("ar_transformations",)
LOCAL_TIER = "local_transformations"

# Keyword arguments the PIPELINE supplies, never the form. Exempt from the
# "required parameter not supplied" check so a correct form is not told to
# declare them. `df` / `inputs` are positional.
POPULATION_ENGINE_SUPPLIED = ("entity_id_column", "reference_date", "random_state")
LABEL_ENGINE_SUPPLIED = ("outcome_name",)
CONDITION_ENGINE_SUPPLIED = ("inputs",)
FEATURE_GROUP_ENGINE_SUPPLIED = ()

# Sections the template ships as `...` or commented out. Filling one in is
# silently ignored by every consumer, which reads as "configured" to the author.
# `validation` is the §4 gate: the gate that actually rejects a dataset is the MRD
# form's. `refresh` drives M3.
TBD_SECTIONS = ("validation", "refresh")

REQUIRED_NON_EMPTY_MSG = "is required and must be non-empty."
POPULATION_STRATEGY_FIELD = "population.strategy"
LABEL_SOURCE_FEATURE_GROUPS_FIELD = "label.source.feature_groups"
POPULATION_ENTITY_ID_COLUMN_FIELD = "population.entity_id_column"

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_]\w*)\}")
# A dotted Python module path: identifier segments only. `module` is form
# input turned into both an import path and, for the local tier, a filesystem
# path segment (_resolve_module) — this rejects `/`, `..`, and anything else
# that isn't a plain dotted identifier before either happens.
_MODULE_PATH_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")

_MISSING = object()


class Issue:
    """One validation finding, at ERROR or WARN."""

    def __init__(self, level, field, message):
        self.level = level
        self.field = field
        self.message = message

    def __str__(self):
        return f"[{self.level}] {self.field}: {self.message}"

    def as_dict(self):
        return {"level": self.level, "field": self.field, "message": self.message}


class ValidationError(Exception):
    """The form is malformed. Carries every ERROR, not just the first."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(
            f"{FORM_FILENAME} rejected with {len(errors)} error(s):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


# --------------------------------------------------------------------------
# Path access
# --------------------------------------------------------------------------


def get_path(mapping, dotted):
    """Fetch a dotted path out of nested dicts, or _MISSING if absent.

    _MISSING is distinct from None: the template uses an explicit null in places,
    and "declared as null" is not the same finding as "never declared".
    """
    node = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def is_blank(value):
    """True for the shapes a template's unfilled placeholder takes.

    "", [], {} and null all count; 0 and False do not — they are values an author
    chose (`masking_window: 0`, `reviewed_by_lead_ds: false`).
    """
    if value is _MISSING or value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)):
        return len(value) == 0
    return False


def _require(issues, form, path, kind=None):
    """One REQUIRED marker in the template: present, non-blank, right type."""
    value = get_path(form, path)
    if is_blank(value):
        issues.append(Issue(ERROR, path, REQUIRED_NON_EMPTY_MSG))
        return False
    if kind is not None and not isinstance(value, kind):
        issues.append(
            Issue(
                ERROR,
                path,
                f"must be {kind.__name__}, got {type(value).__name__} ({value!r}).",
            )
        )
        return False
    return True


# --------------------------------------------------------------------------
# Source queries
# --------------------------------------------------------------------------


def query_block(node):
    """Normalize a `source_query` value to {path, params}."""
    if isinstance(node, str):
        return {"path": node, "params": {}}
    if not isinstance(node, dict):
        return {"path": "", "params": {}}
    return {"path": node.get("path") or "", "params": dict(node.get("params") or {})}


def render_query(text, params, field):
    """Substitute a query's ${PARAMETER_NAME} placeholders.

    Raises ValueError naming every unbound placeholder rather than shipping a
    literal "${COHORT_TABLE}" to Snowflake, where it fails as a syntax error with
    no hint about which form field was blank.
    """
    for key, value in (params or {}).items():
        text = text.replace(f"${{{key}}}", str(value))
    unbound = sorted(set(_PLACEHOLDER_RE.findall(text)))
    if unbound:
        raise ValueError(
            f"still has unbound placeholder(s) {unbound}. Declare them under "
            f"{field}.params (bound: {sorted(params or {})})."
        )
    return text


# --------------------------------------------------------------------------
# The label dispatch block and its nested condition blocks
# --------------------------------------------------------------------------


def label_transformation_block(form):
    """`label.transformation`, or {} if the form left it out.

    Canon carries exactly ONE generic dispatch block here — not one per
    target_type — since only one target_type is ever active for a given model.
    `function` is expected to match `target_type` (e.g. target_type: binary ->
    function: "binary"), but nothing here enforces that by dispatching on
    target_type; the block is read and resolved exactly as declared.
    """
    label = form.get("label") or {}
    block = label.get("transformation")
    return block if isinstance(block, dict) else {}


# Two fields the template repeats/nests but the engine does not take as-is.
_LABEL_PARAM_BRIDGED_KEYS = ("output_target_columns", "maturity")

_MATURITY_KWARGS = {
    "require_mature": "require_mature",
    "horizon_days": "maturity_horizon_days",
    "duration_column": "maturity_duration_column",
    "immature_policy": "maturity_immature_policy",
}


def label_engine_params(form):
    """`label.transformation.params`, bridged to what the target
    `ar_transformations.labels` function actually takes as keyword arguments.

    `output_target_columns` is repeated from `label.output_target_columns` for
    the author's benefit (checked for agreement in `_label_checks`) — the engine
    takes a scalar `outcome_name`, supplied by the pipeline (LABEL_ENGINE_SUPPLIED),
    never by `params`. `maturity` is nested in the template but four flat,
    prefixed keywords in every strategy. Everything else — including nested
    condition blocks like `positive`/`negative` — passes through unchanged: the
    dispatch contract recursing one level deeper is not a nested config schema
    this function needs to know about.
    """
    params = label_transformation_block(form).get("params") or {}
    if not isinstance(params, dict):
        return {}
    out = {k: v for k, v in params.items() if k not in _LABEL_PARAM_BRIDGED_KEYS}
    maturity = params.get("maturity") or {}
    if isinstance(maturity, dict):
        for key, kwarg in _MATURITY_KWARGS.items():
            if key in maturity:
                out[kwarg] = maturity[key]
    return out


def label_engine_block(form):
    """`label.transformation` with `params` bridged to real engine keyword args,
    or {} if the form left the block out.
    """
    block = label_transformation_block(form)
    if not block:
        return {}
    return {**block, "params": label_engine_params(form)}


def _is_dispatch_shaped(value):
    """True for a value that itself looks like a `{module, function, ...}` block."""
    return isinstance(value, dict) and "module" in value and "function" in value


def _condition_blocks_in_list(key, value):
    """Dispatch-shaped entries of one `params` list value, keyed by index."""
    found = {}
    for i, entry in enumerate(value):
        if _is_dispatch_shaped(entry):
            found[f"label.transformation.params.{key}[{i}]"] = entry
    return found


def condition_blocks(form):
    """The per-condition transformation blocks nested inside `label.transformation.params`.

    `positive`/`negative` (binary), `expression` (regression), `relevance`
    (ranking), `event_indicator` (time_to_event), and each entry of `classes`/
    `labels` (multiclass/multilabel) are all full `{module, function,
    source_columns, params}` blocks nested one level inside `params` — the
    dispatch contract recursing, not a nested config schema. Found by shape
    (any params value that itself looks like a dispatch block) rather than by
    target_type, so nothing here hardcodes which families have them or what
    they are named.
    """
    params = label_transformation_block(form).get("params") or {}
    if not isinstance(params, dict):
        return {}
    found = {}
    for key, value in params.items():
        if key == "maturity":
            continue
        if _is_dispatch_shaped(value):
            found[f"label.transformation.params.{key}"] = value
        elif isinstance(value, list):
            found.update(_condition_blocks_in_list(key, value))
    return found


def label_feature_groups(form):
    """`label.source.feature_groups`, or [] if the form left it out.

    Each entry joins a Feature Store group's offline-store columns onto the
    population frame — keyed on (record_id, event_time), an EXACT match, not
    an as-of pick — before `label.transformation` runs, so a raw outcome
    signal that lives in the Feature Store rather than on the population
    source query is available under `label.input_target_columns[0]` the same
    way a population-source-derived one already is.
    """
    label = form.get("label") or {}
    groups = (label.get("source") or {}).get("feature_groups")
    return groups if isinstance(groups, list) else []


def feature_group_transformation_blocks(form):
    """Every `label.source.feature_groups[].transformation` block, with its field."""
    return [
        (
            f"label.source.feature_groups[{index}].transformation",
            group.get("transformation") or {},
        )
        for index, group in enumerate(label_feature_groups(form))
        if isinstance(group, dict)
    ]


def dispatch_blocks(form):
    """Every executable reference the form names, wherever it names one."""
    population = form.get("population") or {}
    return [
        population.get("strategy") or {},
        label_transformation_block(form),
        *condition_blocks(form).values(),
        *(block for _, block in feature_group_transformation_blocks(form)),
    ]


def local_modules(form):
    """The form-local modules the form names, deduplicated.

    One definition, used by validation (is it staged? does it need a
    requirements.txt?) and by the compiler (what must the code channel carry?), so
    the two cannot disagree about which modules are form-local.
    """
    return sorted(
        {
            str(block.get("module") or "")
            for block in dispatch_blocks(form)
            if isinstance(block, dict)
            and str(block.get("module") or "").startswith(LOCAL_TIER)
        }
    )


# --------------------------------------------------------------------------
# Phase 1 — structural
# --------------------------------------------------------------------------


# Population-strategy params that name a real source column rather than
# carrying a literal value — normalized alongside entity_id_column and every
# dispatch block's source_columns. anchor_date_source is included even though
# `anchor_date_type: "fixed"` uses it for a literal date: lowercasing a date
# string is a no-op, so normalizing it unconditionally is safe and avoids
# threading anchor_date_type through this function to decide.
_POPULATION_COLUMN_PARAMS = (
    "entity_id_column",
    "anchor_date_source",
    "window_anchor_column",
    "label_window_event_date_column",
    "observation_window_ends_at",
    "observation_window_ends_at_negative",
    "label_window_ends_at",
    "prediction_window_starts_at",
)


def normalize_identifier_casing(form):
    """Lowercase every identifier the job's own case-folding touches, IN PLACE.

    The Processing job lowercases every column Snowflake returns,
    unconditionally — so an identifier this form declares in any other case
    would validate against the form's own text but bind nothing at real run
    time. Normalizing here, once, before validation runs, makes the two agree.

    Free text (descriptions, tag values, business_unit, Snowflake
    warehouse/database/table names) is untouched — only fields matched
    against real source columns: `population.entity_id_column`,
    `label.input_target_columns` (a blank `label.transformation` reads it as
    a real frame lookup — `population_label.apply_label_strategy`'s
    passthrough path), the population strategy's own column-shaped params,
    and every dispatch block's `source_columns` values (population.strategy,
    label.transformation, and each nested condition block
    `dispatch_blocks()` finds). `label.output_target_columns` is NOT
    normalized here — the job WRITES that column, so its case is the
    author's free choice, not something bound against a real source column.
    """
    if not isinstance(form, dict):
        return

    _normalize_population_entity_id(form)
    _normalize_label_input_columns(form)
    _normalize_feature_group_columns(form)
    _normalize_dispatch_block_columns(form)


def _normalize_population_entity_id(form):
    population = form.get("population")
    if isinstance(population, dict):
        entity_id = population.get("entity_id_column")
        if isinstance(entity_id, str):
            population["entity_id_column"] = entity_id.lower()


def _normalize_label_input_columns(form):
    label = form.get("label")
    if isinstance(label, dict):
        inputs = label.get("input_target_columns")
        if isinstance(inputs, list):
            label["input_target_columns"] = [
                v.lower() if isinstance(v, str) else v for v in inputs
            ]


def _normalize_feature_group_columns(form):
    for group in label_feature_groups(form):
        if not isinstance(group, dict):
            continue
        for field in ("record_id", "event_time"):
            value = group.get(field)
            if isinstance(value, str):
                group[field] = value.lower()
        columns = group.get("feature_columns")
        if isinstance(columns, list):
            group["feature_columns"] = [
                v.lower() if isinstance(v, str) else v for v in columns
            ]


def _normalize_dispatch_block_params(params):
    """Lowercase each of `_POPULATION_COLUMN_PARAMS` present in one block's `params`, in place."""
    if not isinstance(params, dict):
        return
    for key in _POPULATION_COLUMN_PARAMS:
        value = params.get(key)
        if isinstance(value, str):
            params[key] = value.lower()


def _normalize_dispatch_block_source_columns(source_columns):
    """Lowercase every real column name in one block's `source_columns`, in place."""
    if not isinstance(source_columns, dict):
        return
    for arg, column in source_columns.items():
        if isinstance(column, str):
            source_columns[arg] = column.lower()


def _normalize_dispatch_block_columns(form):
    for block in dispatch_blocks(form):
        if not isinstance(block, dict):
            continue
        _normalize_dispatch_block_params(block.get("params"))
        _normalize_dispatch_block_source_columns(block.get("source_columns"))


def _check_required_top_level_sections(issues, form):
    """Flags any REQUIRED_SECTIONS key that is missing from the form."""
    for section in REQUIRED_SECTIONS:
        if section not in form:
            issues.append(
                Issue(ERROR, section, "required top-level section is missing.")
            )


def _check_required_scalar_fields(issues, form):
    """Checks the required, non-blank, correctly-typed identity/population/compute fields."""
    _require(issues, form, "identity.model_name", str)
    _require(issues, form, "identity.business_unit", str)
    _require(issues, form, "identity.description", str)
    _require(issues, form, "identity.owners", list)
    _require(issues, form, "population.entity_type", str)
    _require(issues, form, POPULATION_ENTITY_ID_COLUMN_FIELD, str)
    _require(issues, form, "population.source.source_query.path", str)
    _require(issues, form, "compute.instance_type", str)


def _check_entity_id_lowercase(issues, form):
    """Flags population.entity_id_column when it is not already lowercase."""
    entity_id = get_path(form, POPULATION_ENTITY_ID_COLUMN_FIELD)
    if isinstance(entity_id, str) and entity_id != entity_id.lower():
        issues.append(
            Issue(
                ERROR,
                POPULATION_ENTITY_ID_COLUMN_FIELD,
                f"{entity_id!r} must be lowercase — the job lowercases every source "
                f"column, so this would match nothing.",
            )
        )


def _check_reviewed_by_lead_ds(issues, form):
    """Warns when identity.reviewed_by_lead_ds is declared but not yet true."""
    declared_reviewed = get_path(form, "identity.reviewed_by_lead_ds")
    if declared_reviewed is not _MISSING and not declared_reviewed:
        issues.append(
            Issue(
                WARN,
                "identity.reviewed_by_lead_ds",
                "the HITL cohort/label review gate is not yet true. A Dev run "
                "proceeds; a Prod-bound training run does not.",
            )
        )


def _check_population_access_confirmed(issues, form):
    """Requires population.source.access_confirmed to be true — a hard gate."""
    if get_path(form, "population.source.access_confirmed") is not True:
        issues.append(
            Issue(
                ERROR,
                "population.source.access_confirmed",
                "must be true — the pipeline does not read Gold until the data owner "
                "has granted access. A hard gate.",
            )
        )


def _check_tbd_sections(issues, form):
    """Warns for any section still carrying the template's [TBD] placeholder."""
    for section in TBD_SECTIONS:
        if get_path(form, section) is not _MISSING:
            issues.append(
                Issue(
                    WARN,
                    section,
                    "is declared but nothing reads it yet ([TBD] in the canonical "
                    "template) — it will be ignored at run time.",
                )
            )


def _structural_checks(form):
    issues = []
    _check_required_top_level_sections(issues, form)
    if any(i.level == ERROR for i in issues):
        return issues

    _check_required_scalar_fields(issues, form)
    _check_entity_id_lowercase(issues, form)
    _check_reviewed_by_lead_ds(issues, form)
    _check_population_access_confirmed(issues, form)

    issues.extend(_population_strategy_checks(form))
    issues.extend(_criteria_checks(form))
    issues.extend(_label_checks(form))
    issues.extend(_label_feature_groups_checks(form))
    issues.extend(_storage_checks(form))
    issues.extend(_compute_checks(form))

    _check_tbd_sections(issues, form)
    return issues


def _population_strategy_checks(form):
    """The population strategy's shape and its temporal-parameter coherence.

    The parameters themselves are checked in phase 2 against the real signature.
    What needs checking here is the cross-parameter logic a signature cannot
    express: which anchor / window / masking combinations mean anything together.

    A BLANK `strategy` is PASSTHROUGH, not an error — the same convention
    `label.transformation` already has (see `population_label
    .apply_label_strategy`'s docstring): the population source query's own
    rows are used as-is, with no anchor_date / observation_window /
    label_window / feature_window / masking / label_mature computed or
    written. A model that needs none of AR's windowing (its population
    source query already IS the exact row set it wants) should not have to
    author a trivial identity strategy for it. Every downstream consumer that
    defaults to reading one of those provenance columns (e.g. MRD's
    `pit_join.feature_cutoff` default, `validate_and_write
    .require_full_observability`) becomes that model's own responsibility to
    configure around a cohort with no AR-native provenance columns —
    unchecked here, the same way `label.transformation`'s blank case is not
    cross-checked against MRD's own assumptions either.
    """
    issues = []
    strategy = get_path(form, POPULATION_STRATEGY_FIELD)
    if is_blank(strategy):
        return issues
    if not isinstance(strategy, dict):
        issues.append(
            Issue(
                ERROR,
                POPULATION_STRATEGY_FIELD,
                "must be a mapping with module/function/params keys. The bare "
                "strategy name plus a sibling config block was retired.",
            )
        )
        return issues

    params = strategy.get("params") or {}
    if not isinstance(params, dict):
        issues.append(
            Issue(
                ERROR,
                "population.strategy.params",
                f"must be a mapping, got {type(params).__name__}.",
            )
        )
        return issues

    prefix = "population.strategy.params"
    _check_strategy_anchor_date(issues, params, prefix)
    _check_strategy_window_and_masking_type_required(issues, params, prefix)
    _check_strategy_window_anchor_column(issues, params, prefix)
    _check_strategy_masking_requires_dynamic_window(issues, params, prefix)
    _check_strategy_masking_window(issues, params, prefix)
    _check_strategy_label_window_length(issues, params, prefix)
    return issues


def _check_strategy_anchor_date(issues, params, prefix):
    """Checks anchor_date_type is allowed and anchor_date_source is present."""
    anchor_type = params.get("anchor_date_type")
    if is_blank(anchor_type):
        issues.append(
            Issue(
                ERROR,
                f"{prefix}.anchor_date_type",
                REQUIRED_NON_EMPTY_MSG,
            )
        )
    elif anchor_type not in ALLOWED_ANCHOR_TYPES:
        issues.append(
            Issue(
                ERROR,
                f"{prefix}.anchor_date_type",
                f"{anchor_type!r} is not allowed. Allowed: "
                f"{sorted(ALLOWED_ANCHOR_TYPES)}.",
            )
        )
    if is_blank(params.get("anchor_date_source")):
        kind = "literal date" if anchor_type == "fixed" else "column/expression"
        issues.append(
            Issue(
                ERROR,
                f"{prefix}.anchor_date_source",
                f"is required — for anchor_date_type {anchor_type!r} it carries the "
                f"{kind}.",
            )
        )


def _check_strategy_window_and_masking_type_required(issues, params, prefix):
    """Requires window_type and masking_type to be present and in the allowed set."""
    window_type = params.get("window_type")
    masking_type = params.get("masking_type")
    for field, value in (("window_type", window_type), ("masking_type", masking_type)):
        if is_blank(value):
            issues.append(Issue(ERROR, f"{prefix}.{field}", REQUIRED_NON_EMPTY_MSG))
        elif value not in ALLOWED_WINDOW_TYPES:
            issues.append(
                Issue(
                    ERROR,
                    f"{prefix}.{field}",
                    f"{value!r} is not allowed. Allowed: "
                    f"{sorted(ALLOWED_WINDOW_TYPES)}.",
                )
            )


def _check_strategy_window_anchor_column(issues, params, prefix):
    """window_anchor_column is required for a dynamic window and ignored for a static one.

    window_anchor_column is the per-row date a dynamic feature window measures
    back FROM. Without it the window silently falls back to the shared static
    one, which is the row-level leak this check exists to prevent.
    """
    window_type = params.get("window_type")
    anchor_column = params.get("window_anchor_column")
    if window_type == "dynamic" and is_blank(anchor_column):
        issues.append(
            Issue(
                ERROR,
                f"{prefix}.window_anchor_column",
                "is required when window_type is 'dynamic' — it is the per-row date "
                "the feature window measures back from.",
            )
        )
    if window_type == "static" and not is_blank(anchor_column):
        issues.append(
            Issue(
                WARN,
                f"{prefix}.window_anchor_column",
                "is set but window_type is 'static', which measures back from the "
                "label window's start — this value is ignored.",
            )
        )


def _check_strategy_masking_requires_dynamic_window(issues, params, prefix):
    """A per-row masking draw needs a per-row date to draw around."""
    window_type = params.get("window_type")
    masking_type = params.get("masking_type")
    if masking_type == "dynamic" and window_type != "dynamic":
        issues.append(
            Issue(
                ERROR,
                f"{prefix}.masking_type",
                "'dynamic' requires window_type 'dynamic' — an independent per-row "
                "offset has no per-row date to apply to otherwise.",
            )
        )


def _check_strategy_masking_window(issues, params, prefix):
    """Validates masking_window's shape against masking_type (static int vs dynamic [lo, hi])."""
    masking_type = params.get("masking_type")
    masking_window = params.get("masking_window")
    if masking_type == "static":
        if isinstance(masking_window, bool) or not isinstance(masking_window, int):
            issues.append(
                Issue(
                    ERROR,
                    f"{prefix}.masking_window",
                    f"must be an int (months) when masking_type is 'static', got "
                    f"{masking_window!r}.",
                )
            )
        elif masking_window < 0:
            issues.append(
                Issue(
                    ERROR,
                    f"{prefix}.masking_window",
                    "is measured BACKWARD, so it must be >= 0.",
                )
            )
    elif masking_type == "dynamic":
        ok = (
            isinstance(masking_window, (list, tuple))
            and len(masking_window) == 2
            and all(
                isinstance(v, int) and not isinstance(v, bool) for v in masking_window
            )
            and 0 <= masking_window[0] <= masking_window[1]
        )
        if not ok:
            issues.append(
                Issue(
                    ERROR,
                    f"{prefix}.masking_window",
                    f"must be an inclusive [lo, hi] of non-negative ints with "
                    f"lo <= hi when masking_type is 'dynamic', got {masking_window!r}.",
                )
            )


def _check_strategy_label_window_length(issues, params, prefix):
    """Warns when label_window_length is not set."""
    if is_blank(params.get("label_window_length")):
        issues.append(
            Issue(
                WARN,
                f"{prefix}.label_window_length",
                "is not set. A label read from window membership needs the bounds "
                "this resolves into label_window_start / label_window_end.",
            )
        )


def _check_criteria_field(issues, field, criteria):
    """One of population.inclusion_criteria/exclusion_criteria: a list of non-empty strings."""
    if criteria is None or criteria == []:
        return
    if not isinstance(criteria, list):
        issues.append(
            Issue(
                ERROR,
                f"population.{field}",
                f"must be a list of pandas query expressions, got "
                f"{type(criteria).__name__}.",
            )
        )
        return
    for index, condition in enumerate(criteria):
        if not isinstance(condition, str) or not condition.strip():
            issues.append(
                Issue(
                    ERROR,
                    f"population.{field}[{index}]",
                    f"must be a non-empty string, got {condition!r}.",
                )
            )


def _criteria_checks(form):
    """`population.inclusion_criteria` / `exclusion_criteria`: each entry must
    be a non-empty string. Whether it PARSES as a pandas query expression is
    phase 2's job (`_criteria_syntax_checks`) — that needs pandas, which the
    Lambda this module also runs in does not carry, so it stays out of this
    pandas-free phase.
    """
    issues = []
    population = form.get("population") or {}
    for field in ("inclusion_criteria", "exclusion_criteria"):
        _check_criteria_field(issues, field, population.get(field))
    return issues


def _check_label_target_type(issues, label):
    """label.target_type is required and must name a known label family."""
    target_type = label.get("target_type")
    if is_blank(target_type):
        issues.append(
            Issue(
                ERROR,
                "label.target_type",
                "is required — it names which ar_transformations.labels function "
                "label.transformation is expected to point at.",
            )
        )
    elif target_type not in LABEL_FAMILIES:
        issues.append(
            Issue(
                ERROR,
                "label.target_type",
                f"{target_type!r} is not a known label family. Allowed: "
                f"{sorted(LABEL_FAMILIES)}.",
            )
        )


def _check_label_output_columns(issues, label):
    """label.output_target_columns is required and must be a single-column list.

    Returns the (possibly blank/invalid) output_columns value so later checks
    can cross-reference it without recomputing it.
    """
    output_columns = label.get("output_target_columns")
    if is_blank(output_columns):
        issues.append(
            Issue(
                ERROR,
                "label.output_target_columns",
                REQUIRED_NON_EMPTY_MSG,
            )
        )
    elif not isinstance(output_columns, list) or len(output_columns) != 1:
        # The strategies all write a scalar outcome_name; a multi-column target
        # needs a different target_type (e.g. multilabel emits one column PER
        # label internally, still driven by a single outcome_name).
        issues.append(
            Issue(
                ERROR,
                "label.output_target_columns",
                f"must be a list of exactly one column, got {output_columns!r}.",
            )
        )
    return output_columns


def _check_label_source_location(issues, form):
    """label.source.source_location, if declared, must be an allowed value."""
    source_location = get_path(form, "label.source.source_location")
    if (
        source_location is not _MISSING
        and source_location not in ALLOWED_LABEL_SOURCE_LOCATIONS
    ):
        issues.append(
            Issue(
                ERROR,
                "label.source.source_location",
                f"{source_location!r} is not allowed. Allowed: "
                f"{sorted(ALLOWED_LABEL_SOURCE_LOCATIONS)}.",
            )
        )


def _check_label_passthrough_inputs(issues, label, block):
    """When label.transformation is blank, input_target_columns must be exactly one column.

    Blank means PASSTHROUGH — see label_engine_block: the label already
    exists as a column on the population/label source query, and is carried
    through under label.output_target_columns rather than dispatched. That
    rename is unambiguous only when there is exactly one input column to
    rename FROM, matching the one output column already required above.
    """
    if not is_blank(block):
        return
    inputs = label.get("input_target_columns")
    if not is_blank(inputs) and (not isinstance(inputs, list) or len(inputs) != 1):
        issues.append(
            Issue(
                ERROR,
                "label.input_target_columns",
                f"must be a list of exactly one column when label.transformation "
                f"is blank — the passthrough renames that one column to "
                f"label.output_target_columns[0]. Got {inputs!r}.",
            )
        )


def _check_label_output_agreement(issues, block, output_columns):
    """label.transformation.params.output_target_columns must match label.output_target_columns."""
    declared_output = (
        block.get("params", {}).get("output_target_columns")
        if isinstance(block.get("params"), dict)
        else None
    )
    if (
        not is_blank(output_columns)
        and not is_blank(declared_output)
        and list(declared_output) != list(output_columns)
    ):
        issues.append(
            Issue(
                ERROR,
                "label.transformation.params.output_target_columns",
                f"{declared_output!r} disagrees with label.output_target_columns "
                f"{output_columns!r}. The template repeats the same value in both "
                f"places; make them match.",
            )
        )


def _check_label_maturity(issues, block):
    """label.transformation.params.maturity: require_mature's dependent fields and immature_policy."""
    maturity = (
        block.get("params", {}).get("maturity")
        if isinstance(block.get("params"), dict)
        else None
    )
    if not isinstance(maturity, dict):
        return
    if maturity.get("require_mature"):
        if is_blank(maturity.get("duration_column")):
            issues.append(
                Issue(
                    ERROR,
                    "label.transformation.params.maturity.duration_column",
                    "is required when require_mature is true — the gate has no "
                    "follow-up length to measure against without it.",
                )
            )
        horizon = maturity.get("horizon_days")
        if is_blank(horizon) or not isinstance(horizon, int) or horizon <= 0:
            issues.append(
                Issue(
                    ERROR,
                    "label.transformation.params.maturity.horizon_days",
                    f"must be an int > 0 when require_mature is true, got "
                    f"{horizon!r}.",
                )
            )
    policy = maturity.get("immature_policy")
    if not is_blank(policy) and policy not in ALLOWED_IMMATURE_POLICY:
        issues.append(
            Issue(
                ERROR,
                "label.transformation.params.maturity.immature_policy",
                f"{policy!r} is not allowed. Allowed: "
                f"{sorted(ALLOWED_IMMATURE_POLICY)}.",
            )
        )


def _label_checks(form):
    """`label.target_type`, its single output column, and the transformation block."""
    issues = []
    label = form.get("label") or {}

    _check_label_target_type(issues, label)
    _require(issues, form, "label.input_target_columns", list)
    output_columns = _check_label_output_columns(issues, label)
    _check_label_source_location(issues, form)

    block = label.get("transformation")
    if block is not None and not isinstance(block, dict):
        issues.append(
            Issue(
                ERROR,
                "label.transformation",
                f"must be a mapping with module/function/params keys, got "
                f"{type(block).__name__}.",
            )
        )
        return issues

    _check_label_passthrough_inputs(issues, label, block)

    block = block or {}
    _check_label_output_agreement(issues, block, output_columns)
    _check_label_maturity(issues, block)
    return issues


def _label_feature_groups_checks(form):
    """`label.source.feature_groups`: each entry's join keys and output column.

    Only meaningful when `label.source.source_location` is `feature_groups` — a
    populated list under any other location is dead config, the same "declared
    but read by nothing" case `_label_checks` does not otherwise flag, so it is
    a WARN rather than an ERROR.

    Each entry's `transformation` runs against the FRAME (Pattern B, source
    columns bound from the joined-in feature_columns), and writes ONE column —
    `label.output_target_columns[0]`, the same field `label.transformation`
    itself writes to. This form declares no feature_groups at all when the
    label is computed entirely from population-source columns, so the list is
    only required at all when source_location asks for it.
    """
    issues = []
    label = form.get("label") or {}
    source_location = (label.get("source") or {}).get("source_location")
    groups = label_feature_groups(form)

    if source_location != "feature_groups":
        _check_feature_groups_dead_config(issues, groups, source_location)
        return issues

    if not groups:
        issues.append(
            Issue(
                ERROR,
                LABEL_SOURCE_FEATURE_GROUPS_FIELD,
                "is required and must name at least one feature group when "
                "label.source.source_location is 'feature_groups'.",
            )
        )
        return issues

    output_columns = label.get("output_target_columns")
    _check_feature_groups_single_output_column(issues, groups, output_columns)

    for index, group in enumerate(groups):
        _check_feature_group_entry(issues, form, index, group)
    return issues


def _check_feature_groups_dead_config(issues, groups, source_location):
    """Warns when feature_groups is populated but source_location does not read it."""
    if groups:
        issues.append(
            Issue(
                WARN,
                LABEL_SOURCE_FEATURE_GROUPS_FIELD,
                f"is declared but label.source.source_location is "
                f"{source_location or '(blank)'!r}, so this list is read by "
                f"nothing.",
            )
        )


def _check_feature_groups_single_output_column(issues, groups, output_columns):
    """Rejects multiple feature_groups entries sharing the single output column."""
    if len(groups) > 1 and not is_blank(output_columns):
        issues.append(
            Issue(
                ERROR,
                LABEL_SOURCE_FEATURE_GROUPS_FIELD,
                f"declares {len(groups)} entries, but label.output_target_columns "
                "names only one destination column — each entry's transformation "
                "would write to the same column, silently overwriting the ones "
                "before it. Declare exactly one feature group entry.",
            )
        )


def _check_feature_group_entry(issues, form, index, group):
    """One label.source.feature_groups[] entry: its join keys, columns, and transformation."""
    field = f"label.source.feature_groups[{index}]"
    if not isinstance(group, dict):
        issues.append(Issue(ERROR, field, "must be a mapping."))
        return

    if is_blank(group.get("feature_group_name")):
        issues.append(
            Issue(
                ERROR,
                f"{field}.feature_group_name",
                "is required — it names the registered feature group the "
                "join reads.",
            )
        )

    record_id = group.get("record_id")
    if is_blank(record_id):
        issues.append(
            Issue(
                ERROR,
                f"{field}.record_id",
                "is required — it must match population.entity_id_column, "
                "the column the exact-match join keys on.",
            )
        )
    else:
        entity_id = get_path(form, POPULATION_ENTITY_ID_COLUMN_FIELD)
        if not is_blank(entity_id) and record_id != entity_id:
            issues.append(
                Issue(
                    ERROR,
                    f"{field}.record_id",
                    f"{record_id!r} does not match population.entity_id_column "
                    f"{entity_id!r} — the join has no other key to match rows on.",
                )
            )

    if is_blank(group.get("event_time")):
        issues.append(
            Issue(
                ERROR,
                f"{field}.event_time",
                "is required — the exact-match join keys on (record_id, "
                "event_time) against the population frame's own column of "
                "the same name.",
            )
        )

    columns = group.get("feature_columns")
    if is_blank(columns):
        issues.append(
            Issue(
                ERROR,
                f"{field}.feature_columns",
                "is required and must be non-empty — it names the offline "
                "store columns the join reads.",
            )
        )
    elif not isinstance(columns, list):
        issues.append(
            Issue(
                ERROR,
                f"{field}.feature_columns",
                f"must be a list of columns, got {type(columns).__name__}.",
            )
        )

    transformation = group.get("transformation")
    if is_blank(transformation):
        issues.append(
            Issue(
                ERROR,
                f"{field}.transformation",
                "is required — it computes label.output_target_columns[0] "
                "from this entry's joined-in feature_columns.",
            )
        )
    elif not isinstance(transformation, dict):
        issues.append(
            Issue(
                ERROR,
                f"{field}.transformation",
                f"must be a mapping with module/function/params keys, got "
                f"{type(transformation).__name__}.",
            )
        )


def _check_storage_snowflake_required_fields(issues, snowflake):
    """account/database/schema/table are required when storage.snowflake.enabled is true."""
    for field in ("account", "database", "schema", "table"):
        if is_blank(snowflake.get(field)):
            issues.append(
                Issue(
                    ERROR,
                    f"storage.snowflake.{field}",
                    "is required when storage.snowflake.enabled is true.",
                )
            )


def _check_storage_snowflake_access_confirmed(issues, snowflake):
    """storage.snowflake.access_confirmed must be true when enabled — a hard gate."""
    if snowflake.get("access_confirmed") is not True:
        issues.append(
            Issue(
                ERROR,
                "storage.snowflake.access_confirmed",
                "must be true when storage.snowflake.enabled is true. A hard gate.",
            )
        )


def _check_storage_snowflake_write_mode(issues, snowflake):
    """storage.snowflake.write_mode, if set, must be an allowed value."""
    mode = snowflake.get("write_mode")
    if not is_blank(mode) and mode not in ALLOWED_WRITE_MODES:
        issues.append(
            Issue(
                ERROR,
                "storage.snowflake.write_mode",
                f"{mode!r} is not allowed. Allowed: {sorted(ALLOWED_WRITE_MODES)}.",
            )
        )


def _check_storage_snowflake_warehouse_size(issues, snowflake):
    """storage.snowflake.warehouse_size, if set, must be an allowed size."""
    size = snowflake.get("warehouse_size")
    if not is_blank(size) and size not in ALLOWED_WAREHOUSE_SIZES:
        issues.append(
            Issue(
                ERROR,
                "storage.snowflake.warehouse_size",
                f"{size!r} is not allowed. Declare the size only — the platform "
                f"composes the warehouse name per environment. Allowed: "
                f"{sorted(ALLOWED_WAREHOUSE_SIZES)}.",
            )
        )


def _storage_checks(form):
    """The storage block. The Snowflake fields are "REQUIRED if enabled"."""
    issues = []
    storage = form.get("storage") or {}
    snowflake = storage.get("snowflake") or {}

    if snowflake.get("enabled"):
        _check_storage_snowflake_required_fields(issues, snowflake)
        _check_storage_snowflake_access_confirmed(issues, snowflake)
        _check_storage_snowflake_write_mode(issues, snowflake)
        _check_storage_snowflake_warehouse_size(issues, snowflake)
    return issues


def _compute_checks(form):
    issues = []
    count = (form.get("compute") or {}).get("instance_count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        issues.append(
            Issue(
                ERROR,
                "compute.instance_count",
                f"{count!r} must be a positive integer.",
            )
        )
    return issues


# --------------------------------------------------------------------------
# Phase 2 — needs the staged intake directory
# --------------------------------------------------------------------------


def _check_criteria_field_syntax(issues, pd, empty, field, criteria):
    """Parses every condition in one criteria field as a pandas query expression."""
    for index, condition in enumerate(criteria or []):
        if not isinstance(condition, str) or not condition.strip():
            continue  # already reported by _criteria_checks (phase 1)
        expr = condition if field == "inclusion_criteria" else f"not ({condition})"
        try:
            empty.query(expr)
        except pd.errors.UndefinedVariableError:
            pass  # the real columns don't exist on an empty frame — expected
        except Exception as exc:  # NOSONAR - a query string is DS-authored
            # input; any parse failure is this entry's rejection, not a bug.
            issues.append(
                Issue(
                    ERROR,
                    f"population.{field}[{index}]",
                    f"{condition!r} does not parse as a pandas query "
                    f"expression: {exc}",
                )
            )


def _criteria_syntax_checks(form):
    """`population.inclusion_criteria` / `exclusion_criteria`: each entry
    actually PARSES as a pandas query expression.

    Run against an EMPTY DataFrame — the real source columns are not known
    until the Snowflake read, so the only thing checkable here is syntax, not
    column names. `UndefinedVariableError` (a real column that does not exist
    yet, on an empty frame) is therefore expected and NOT an error; a
    `SyntaxError`/`TokenError`/anything else `.query()` raises on malformed
    input is. Needs pandas — a phase-2-only dependency, same reason
    `check_dispatch`'s module resolution needs the staged intake directory —
    so this stays out of `_criteria_checks` (phase 1, which the Lambda also
    runs with no pandas installed).
    """
    try:
        import pandas as pd
    except ImportError:
        return [
            Issue(
                WARN,
                "population.inclusion_criteria",
                "could not be syntax-checked: pandas is not installed here. A "
                "malformed expression surfaces in the Processing job instead.",
            )
        ]

    issues = []
    empty = pd.DataFrame()
    population = form.get("population") or {}
    for field in ("inclusion_criteria", "exclusion_criteria"):
        _check_criteria_field_syntax(issues, pd, empty, field, population.get(field))
    return issues


def is_snowpark_source(path):
    """True for a `source_query.path` naming a Snowpark `.py` source rather
    than a `.sql` one — the two forms `_check_query`/the compiler treat
    differently: a `.sql` file is TEXT, `${...}`-rendered and executed
    verbatim; a `.py` file is a module implementing
    `ar_transformations.snowpark.base.SnowparkSource`, and `params` reaches
    it as a dict argument (`get_dataframe(session, params)`), never as a
    textual substitution — a Python source has its own way of reading
    configuration and does not need one grafted on.
    """
    return str(path).endswith(".py")


def _check_query(intake_dir, block, field):
    """One source_query block: the file is staged, and — for a `.sql`
    file — every ${...} binds.

    A `.py` (Snowpark) file is checked for presence and non-blankness only:
    its `params` are consumed as a dict at run time, not textually
    substituted, so `render_query`'s check does not apply, and real
    signature checking is out of scope here the same way a dispatch
    block's `module`/`function` are (`_check_module` never imports either).
    """
    if not block["path"]:
        return []
    path = os.path.join(intake_dir, block["path"])
    if not os.path.exists(path):
        return [
            Issue(
                ERROR,
                f"{field}.path",
                f"{block['path']!r} is not in the staged intake directory. Stage the "
                f"sql/ sibling alongside the form.",
            )
        ]
    with open(path) as handle:
        text = handle.read()
    if not text.strip():
        return [Issue(ERROR, f"{field}.path", f"{block['path']!r} is empty.")]
    if is_snowpark_source(block["path"]):
        return []
    try:
        render_query(text, block["params"], field)
    except ValueError as exc:
        return [Issue(ERROR, f"{field}.params", f"{block['path']} {exc}")]
    return []


def is_dotted_module_path(module_name):
    """Whether `module_name` is a clean dotted Python module path.

    Shared by the launcher-side validator (`_check_module`, below) and the
    Processing job's own `engine_dispatch.resolve`: both turn a dispatch
    block's `module` — form input — into an import path, and the local tier
    additionally turns it into a filesystem path segment. Rejecting `/`, `..`
    and anything else that isn't a plain identifier chain here means neither
    caller has to re-derive the same check.
    """
    return bool(_MODULE_PATH_RE.match(module_name))


def _check_module(module_name, intake_dir, field):
    """Check a dispatch block's `module` WITHOUT importing it.

    Returns None when the name is well formed and names a tier this pipeline
    stages, or the Issue explaining why not.
    """
    if not is_dotted_module_path(module_name):
        return Issue(
            ERROR,
            f"{field}.module",
            f"{module_name!r} must be a dotted Python module path (letters, "
            f"digits, underscores, separated by '.') — it names an importable "
            f"module and, for the local tier, a path segment beside the form.",
        )

    root = module_name.split(".")[0]

    if root == LOCAL_TIER:
        relative = os.path.join(*module_name.split(".")) + ".py"
        if not os.path.exists(os.path.join(intake_dir, relative)):
            return Issue(
                ERROR,
                f"{field}.module",
                f"{module_name!r} is a form-local tier, so it must be staged at "
                f"{relative} beside the form; it is not there.",
            )
        return None

    if root not in PACKAGED_TIERS:
        return Issue(
            ERROR,
            f"{field}.module",
            f"{module_name!r} is in neither a packaged tier {sorted(PACKAGED_TIERS)} "
            f"nor the form-local tier {LOCAL_TIER!r}. Only those are staged into the "
            f"Processing image, so a module elsewhere would not resolve at run time.",
        )

    return None


def check_dispatch(block, field, intake_dir):
    """Validate one {module, function, source_columns, params} block.

    In order, stopping when a later check would be meaningless: the block is a
    mapping with a non-empty module and function; the module resolves in a staged
    tier; the function exists and is callable; `source_columns` binds argument
    names to lowercase columns; and `params` supplies every required keyword
    argument and no unknown one.

    This is what turns AC-3/AC-4's "unrecognized value" into a rejection before
    the Processing job and the Snowflake read.
    """
    if not isinstance(block, dict):
        return [
            Issue(
                ERROR,
                field,
                f"must be a mapping with module/function keys, got "
                f"{type(block).__name__}.",
            )
        ]

    issues = []
    module_name = str(block.get("module") or "").strip()
    function_name = str(block.get("function") or "").strip()
    _check_dispatch_names_present(issues, field, module_name, function_name)
    _check_dispatch_source_columns(issues, field, block.get("source_columns"))
    _check_dispatch_params_shape(issues, field, block.get("params"))

    if not module_name or not function_name:
        return issues

    problem = _check_module(module_name, intake_dir, field)
    if problem:
        issues.append(problem)
    return issues


def _check_dispatch_names_present(issues, field, module_name, function_name):
    """Requires a dispatch block's module and function names to be non-empty."""
    if not module_name:
        issues.append(Issue(ERROR, f"{field}.module", REQUIRED_NON_EMPTY_MSG))
    if not function_name:
        issues.append(Issue(ERROR, f"{field}.function", REQUIRED_NON_EMPTY_MSG))


def _check_dispatch_source_columns(issues, field, source_columns):
    """Validates source_columns binds each argument to a non-empty, lowercase column.

    `source_columns` binds an `inputs` dict for a Pattern B feature-style
    transformation (dispatched via `ar_transformations.utils.dispatch.
    call_transformation`, which builds `inputs` from it). A population
    strategy or label transformation instead takes the whole `df` — dispatched
    via `engine_dispatch.call`, which never reads `source_columns` — so an
    empty/blank block here is normal, not an omission, and is only checked
    when the author actually declared bindings.
    """
    if is_blank(source_columns):
        return
    if not isinstance(source_columns, dict):
        issues.append(
            Issue(
                ERROR,
                f"{field}.source_columns",
                f"must be a mapping of the function's argument name to the real "
                f"column, got {type(source_columns).__name__}.",
            )
        )
        return
    for argument, column in source_columns.items():
        text = str(column or "")
        if not text.strip():
            issues.append(
                Issue(
                    ERROR,
                    f"{field}.source_columns.{argument}",
                    "is bound to an empty column name.",
                )
            )
        elif text != text.lower():
            issues.append(
                Issue(
                    ERROR,
                    f"{field}.source_columns.{argument}",
                    f"{text!r} must be lowercase — the job lowercases every "
                    f"source column, so this would bind nothing.",
                )
            )


def _check_dispatch_params_shape(issues, field, params):
    """Requires a dispatch block's params, if present, to be a mapping."""
    if not isinstance(params or {}, dict):
        issues.append(
            Issue(
                ERROR,
                f"{field}.params",
                f"must be a mapping, got {type(params).__name__}.",
            )
        )


def _check_source_queries(issues, form, intake_dir):
    """Checks the population and label source_query blocks are staged and bind."""
    population = form.get("population") or {}
    label = form.get("label") or {}
    issues.extend(
        _check_query(
            intake_dir,
            query_block((population.get("source") or {}).get("source_query")),
            "population.source.source_query",
        )
    )
    issues.extend(
        _check_query(
            intake_dir,
            query_block(
                ((label.get("source") or {}).get("snowflake") or {}).get("source_query")
            ),
            "label.source.snowflake.source_query",
        )
    )


def _check_compute_dockerfile_staged(issues, form, intake_dir):
    """compute.dockerfile, if declared, must be in the staged intake directory.

    §6 compute is BUILD-step input, not run-time: build_and_push.sh reads the
    dockerfile and stages local_requirements into the image, as the
    feature-ingestion runbook does. The run never builds, so the only useful check
    is that the build step will find what the form names.
    """
    compute = form.get("compute") or {}
    dockerfile = str(compute.get("dockerfile") or "")
    if dockerfile and not os.path.exists(os.path.join(intake_dir, dockerfile)):
        issues.append(
            Issue(
                ERROR,
                "compute.dockerfile",
                f"{dockerfile!r} is declared but not in the staged intake directory, "
                f"so build_and_push.sh would not find it.",
            )
        )


def _check_compute_local_requirements_declared(issues, form):
    """Warns when a local_transformations module is named but local_requirements is blank.

    local_requirements is REPO-relative, not form-relative — it is a shared
    requirements.txt a form points at, so it legitimately sits above the staged
    directory and its path cannot be checked from here. What is checkable is the
    template's conditional marker, "Required if local transformations are used":
    a form-local module whose third-party deps were never installed fails on
    import inside the job. Blank is often correct (a pure-stdlib transform), so
    this is a warning.
    """
    compute = form.get("compute") or {}
    if local_modules(form) and not str(compute.get("local_requirements") or ""):
        issues.append(
            Issue(
                WARN,
                "compute.local_requirements",
                "is blank but this form names a local_transformations module. If that "
                "module imports anything the image does not already carry, declare the "
                "requirements.txt for build_and_push.sh to install.",
            )
        )


def _check_dispatch_blocks_resolve(issues, form, intake_dir):
    """Resolves every dispatch block the form names: strategy, label, conditions, feature groups.

    A blank strategy is passthrough (_population_strategy_checks already
    accepted it) — check_dispatch has nothing to resolve against a blank
    block and would otherwise reject it as a malformed one.
    """
    population = form.get("population") or {}
    strategy_block = population.get("strategy")
    if not is_blank(strategy_block):
        issues.extend(
            check_dispatch(strategy_block, POPULATION_STRATEGY_FIELD, intake_dir)
        )

    label_block = label_engine_block(form)
    if label_block:
        issues.extend(check_dispatch(label_block, "label.transformation", intake_dir))
    for field, block in sorted(condition_blocks(form).items()):
        issues.extend(check_dispatch(block, field, intake_dir))

    for field, block in feature_group_transformation_blocks(form):
        if is_blank(block):
            continue
        issues.extend(check_dispatch(block, field, intake_dir))


def _resolved_checks(form, intake_dir):
    issues = []
    issues.extend(_criteria_syntax_checks(form))
    _check_source_queries(issues, form, intake_dir)
    _check_compute_dockerfile_staged(issues, form, intake_dir)
    _check_compute_local_requirements_declared(issues, form)
    _check_dispatch_blocks_resolve(issues, form, intake_dir)
    return issues


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def validate(form, intake_dir):
    """Every finding in the form. No ERROR means it compiles."""
    if not isinstance(form, dict):
        return [Issue(ERROR, FORM_FILENAME, "must parse to a mapping.")]

    normalize_identifier_casing(form)

    issues = _structural_checks(form)
    if any(i.level == ERROR for i in issues):
        return issues
    issues.extend(_resolved_checks(form, intake_dir))
    return issues


def validate_or_raise(form, intake_dir):
    """Raise ValidationError carrying every ERROR; return the WARNs to be logged."""
    issues = validate(form, intake_dir)
    errors = [i for i in issues if i.level == ERROR]
    if errors:
        raise ValidationError(errors)
    return [i for i in issues if i.level == WARN]
