"""Row filters for a feature's `transformation.filters` block.

Applies BEFORE a transformation's `source_columns` are bound and its function
is called (see `transform_and_ingest.apply_transformation`) — the filter scopes
WHICH ROWS a feature is computed over, most useful for a Pattern C dynamic
feature whose expansion needs a narrower slice per combination (e.g. "only
this brand's claims") than the source query itself returns.

One generic tree, three logical operators (`all`/`any`/`not`) over leaf
comparisons — never `eval`/`exec`: every operator is an explicit branch over a
fixed allowlist, so a filter block can only ever do what this file's functions
implement.

    filters:
      all:
        - column: "status"
          operator: "eq"
          value: "active"
        - any:
            - column: "state"
              operator: "in"
              value: ["NY", "NJ"]
            - column: "specialty"
              operator: "eq"
              value: "Cardiology"

Ordinary comparisons never match a null (a missing `status` does not satisfy
`not_in: [inactive]`) — use `is_null`/`not_null` for an explicit null check,
the same reasoning `ar_transformations`' cast transforms use for a value that
fails to parse.
"""

LOGICAL_OPERATORS = ("all", "any", "not")

COMPARISON_OPERATORS = (
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "between",
    "is_null",
    "not_null",
)

# Every operator except these two takes a `value`.
_NO_VALUE_OPERATORS = ("is_null", "not_null")
# These two take a LIST rather than a scalar.
_LIST_VALUE_OPERATORS = ("in", "not_in")


class FilterError(ValueError):
    """A `filters` block is malformed, or could not be evaluated."""


def is_filter_leaf(node):
    """True for a comparison node ({column, operator, value}), false for a
    logical node (all/any/not) or anything malformed."""
    return isinstance(node, dict) and "column" in node


def _logical_key(node, field):
    """The one logical operator `node` declares, or None for a leaf.

    Raises FilterError if `node` mixes a logical key with anything else, or
    declares more than one.
    """
    if not isinstance(node, dict):
        raise FilterError(f"{field} must be a mapping, got {type(node).__name__}.")
    present = [key for key in LOGICAL_OPERATORS if key in node]
    if not present:
        return None
    if len(present) > 1:
        raise FilterError(
            f"{field} declares more than one logical operator: {sorted(present)}. "
            "Nest them instead — one operator per node."
        )
    if len(node) > 1:
        extra = sorted(set(node) - set(present))
        raise FilterError(
            f"{field} cannot combine '{present[0]}' with other keys: {extra}."
        )
    return present[0]


def _validate_not_node(node, field):
    """Errors for a `not` logical node: exactly one nested filter mapping."""
    child = node["not"]
    if not isinstance(child, dict):
        return [
            (f"{field}.not", f"must be one filter mapping, got {type(child).__name__}.")
        ]
    return validate_filter_node(child, f"{field}.not")


def _validate_all_any_node(node, operator, field):
    """Errors for an `all`/`any` logical node: a non-empty list of children."""
    children = node[operator]
    if not isinstance(children, list) or not children:
        return [
            (f"{field}.{operator}", "must be a non-empty list of filter conditions.")
        ]
    errors = []
    for index, child in enumerate(children):
        errors.extend(validate_filter_node(child, f"{field}.{operator}[{index}]"))
    return errors


def _validate_leaf_keys(node, field):
    """Errors for a leaf's key set, before its values are inspected.

    A missing required key short-circuits — the unexpected-key check below it
    is skipped, matching how a leaf missing `operator` has nothing meaningful
    to say about its other keys yet.
    """
    allowed_keys = {"column", "operator", "value"}
    required_keys = {"column", "operator"}
    missing = required_keys - set(node)
    if missing:
        return [(field, f"is missing required key(s): {sorted(missing)}.")]
    unexpected = set(node) - allowed_keys
    if unexpected:
        return [(field, f"has unexpected key(s): {sorted(unexpected)}.")]
    return []


def _validate_leaf_value(node, comparison, field):
    """Errors for a leaf's `value`, once its `operator` is known-valid."""
    has_value = "value" in node
    if comparison in _NO_VALUE_OPERATORS and has_value:
        return [(f"{field}.value", f"must be omitted for operator '{comparison}'.")]
    if comparison not in _NO_VALUE_OPERATORS and not has_value:
        return [(f"{field}.value", f"is required for operator '{comparison}'.")]
    if not has_value:
        return []

    value = node["value"]
    if comparison in _LIST_VALUE_OPERATORS:
        if not isinstance(value, list) or not value:
            return [
                (
                    f"{field}.value",
                    f"must be a non-empty list for operator '{comparison}'.",
                )
            ]
    elif comparison == "between":
        if not isinstance(value, list) or len(value) != 2:
            return [
                (f"{field}.value", "must be a two-item list for operator 'between'.")
            ]
    elif isinstance(value, list):
        return [(f"{field}.value", f"must be a scalar for operator '{comparison}'.")]
    return []


def _validate_leaf_node(node, field):
    """Every structural error in one leaf comparison node."""
    errors = _validate_leaf_keys(node, field)
    if {"column", "operator"} - set(node):
        return errors

    column = node.get("column")
    if not isinstance(column, str) or not column.strip():
        errors.append((f"{field}.column", "must be a non-empty string."))

    comparison = str(node.get("operator") or "").strip().lower()
    if comparison not in COMPARISON_OPERATORS:
        errors.append(
            (
                f"{field}.operator",
                f"{node.get('operator')!r} is not allowed. Allowed: {sorted(COMPARISON_OPERATORS)}.",
            )
        )
        return errors

    errors.extend(_validate_leaf_value(node, comparison, field))
    return errors


def validate_filter_node(node, field):
    """Every structural error in one `filters` node and its children, as
    `(field, message)` pairs — the same shape `intake_schema.Error` takes.

    Runs with no DataFrame — the same "shape only" split every other check in
    this module follows (`_validate_feature_transform`, `check_dispatch`'s own
    module/function checks before it resolves anything). A column that does
    not exist in the source query is NOT caught here — a source query's
    columns are never known before the Snowflake read; that check is
    `_comparison_mask`'s job, at run time.
    """
    try:
        operator = _logical_key(node, field)
    except FilterError as exc:
        return [(field, str(exc))]

    if operator == "not":
        return _validate_not_node(node, field)
    if operator in ("all", "any"):
        return _validate_all_any_node(node, operator, field)
    return _validate_leaf_node(node, field)


# --------------------------------------------------------------------------
# Runtime evaluation — pandas is a Processing-job dependency only (see
# src/images/job-feature-ingestion/requirements.txt), never the Lambda's, so
# every function below imports it lazily rather than at module scope. The
# validator above never reaches this half of the file.
# --------------------------------------------------------------------------


def apply_filters(df, filters, field):
    """`df` restricted to the rows `filters` keeps, or a copy of `df` unchanged
    if `filters` is blank.

    `field` is the dotted YAML path this block came from (e.g.
    "features[3].${brand}_count.transformation.filters"), so a runtime
    failure — an unknown column the source query dropped after this ran clean
    at validation time — still names which feature's filter block produced it.
    """
    return df.loc[matching_mask(df, filters, field)].copy()


def matching_mask(df, filters, field):
    """The boolean Series `filters` resolves to over `df`, or all-True if
    `filters` is blank — the row-selection `apply_filters` applies, exposed on
    its own for a caller that needs to act on non-matching rows (e.g. null
    them out) rather than drop them outright."""
    if not filters:
        import pandas as pd

        return pd.Series(True, index=df.index)
    return _filter_mask(df, filters, field)


def _filter_mask(df, node, field):
    """The boolean Series one filter node (and its children) resolves to."""
    operator = _logical_key(node, field)

    if operator == "not":
        return ~_filter_mask(df, node["not"], f"{field}.not")

    if operator == "all":
        import pandas as pd

        mask = pd.Series(True, index=df.index)
        for index, child in enumerate(node["all"]):
            mask &= _filter_mask(df, child, f"{field}.all[{index}]")
        return mask

    if operator == "any":
        import pandas as pd

        mask = pd.Series(False, index=df.index)
        for index, child in enumerate(node["any"]):
            mask |= _filter_mask(df, child, f"{field}.any[{index}]")
        return mask

    return _comparison_mask(df, node, field)


def _comparison_mask(df, node, field):
    """The boolean Series one leaf {column, operator, value} resolves to.

    An ordinary comparison never matches a null: `series.eq`/`.isin`/etc.
    already return False on a null operand, but `ne`/`not_in` would otherwise
    return True for one (pandas' negation of False-on-null is True-on-null),
    which reads as "a missing value differs from this" — the same false
    positive `is_null`/`not_null` exist to make explicit instead.
    """
    column = node["column"]
    if column not in df.columns:
        raise FilterError(
            f"{field}.column {column!r} is not a column the source query returned. "
            f"Available: {sorted(df.columns)}."
        )
    comparison = str(node["operator"]).strip().lower()
    series = df[column]

    if comparison == "is_null":
        return series.isna()
    if comparison == "not_null":
        return series.notna()

    value = node["value"]
    non_null = series.notna()
    if comparison == "eq":
        return non_null & series.eq(value)
    if comparison == "ne":
        return non_null & series.ne(value)
    if comparison == "gt":
        return non_null & series.gt(value)
    if comparison == "gte":
        return non_null & series.ge(value)
    if comparison == "lt":
        return non_null & series.lt(value)
    if comparison == "lte":
        return non_null & series.le(value)
    if comparison == "in":
        return non_null & series.isin(value)
    if comparison == "not_in":
        return non_null & ~series.isin(value)
    if comparison == "between":
        lo, hi = value
        return non_null & series.between(lo, hi, inclusive="both")

    raise FilterError(f"{field}.operator {comparison!r} is not implemented.")
