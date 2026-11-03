"""Call the transformations distribution from a model-ready-dataset stage script.

The runtime half of the dispatch contract stage 1's validator checks a form against:
it resolves the same `{module, function, params}` block the same way and actually
calls it. Keeping the two halves shaped alike is what makes a form that validated
also runnable — see `src/common/mrd_schema.py`.

`src/processing/cohort-label/engine_dispatch.py` carries its own `resolve`/`describe`
pair, near-identical to these. The duplication is deliberate and matches the note at
the top of `mrd_schema.py`: merging the two is deferred to the refactoring sprint, not
done as a side effect of this build. The call shapes genuinely differ — a cohort-label
strategy takes pipeline-supplied `engine_kwargs`, while nothing is injected here
(handoff §4.1), and the column form below has no counterpart there at all.

Nothing here knows how many strategies exist, so AR-18-a-3's later additions plug in
with no change to this module.

Failures raise `SystemExit` naming the YAML field rather than letting a bare
`ModuleNotFoundError` surface: the traceback names only the module, and a compiled
intake carries several dispatch blocks, so an author cannot otherwise tell which field
produced it. Inside the container this is fatal by definition — the job cannot
proceed — which is why it exits rather than collecting findings the way the validator
does.
"""

import importlib


def resolve(block, field):
    """Resolve a dispatch block's {module, function} to the real callable."""
    if not isinstance(block, dict):
        raise SystemExit(
            f"[fail] {field} must be a mapping with module/function keys, got "
            f"{type(block).__name__}."
        )

    module_name = str(block.get("module", "") or "").strip()
    function_name = str(block.get("function", "") or "").strip()
    if not module_name or not function_name:
        raise SystemExit(
            f"[fail] {field} needs both module and function; got "
            f"module={module_name!r} function={function_name!r}."
        )

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Says which image is stale rather than which import failed: the
        # transformations distribution is baked into the image from source, so a
        # strategy added after the build is not importable.
        raise SystemExit(
            f"[fail] {field}.module {module_name!r} is not importable inside the "
            f"Processing image ({exc}). The transformations distribution and any "
            f"form-local tier are baked by "
            f"docs/runbooks/model-ready-dataset/build_and_push.sh — if this module "
            f"was added recently, rebuild and push the image."
        ) from exc

    func = getattr(module, function_name, None)
    if func is None:
        available = sorted(
            name
            for name in getattr(module, "__all__", dir(module))
            if not name.startswith("_")
        )
        raise SystemExit(
            f"[fail] {field}.function {function_name!r} does not exist in "
            f"{module_name!r}. Available: {available}"
        )
    if not callable(func):
        raise SystemExit(
            f"[fail] {field}.function {module_name}.{function_name} is not callable."
        )
    return func


def _params(block, field):
    """A block's `params`, refusing a shape that cannot be splatted."""
    params = block.get("params") or {}
    if not isinstance(params, dict):
        raise SystemExit(
            f"[fail] {field}.params must be a mapping, got {type(params).__name__}."
        )
    return params


def call(block, field, frame):
    """Call a whole-frame strategy: `sampling`, `splits` or the validation gate.

    The frame form of the contract — `module.function(df, **params)`. NOTHING is
    injected by the pipeline, which is why `params` carries `entity_id_column` and
    `target_column` even though both are declared elsewhere in the form. The
    cohort-label sibling's `call` takes `**engine_kwargs` for exactly the injection
    this contract does not do; the two are separate on purpose (handoff §4.2).

    Returns whatever the strategy returns — a DataFrame for sampling, a
    `{train, validation, holdout}` dict for splits, nothing for the validation gate,
    which raises instead. The caller checks the shape it expects, because only the
    caller knows which of the three it asked for.
    """
    func = resolve(block, field)
    params = _params(block, field)
    try:
        return func(frame, **params)
    except TypeError as exc:
        raise SystemExit(
            f"[fail] {field} could not be called: {exc}. params keys are "
            f"{sorted(params)}; the dataset is passed as the single positional "
            f"argument and nothing else is injected."
        ) from exc


def call_source_columns(block, field, frame):
    """Call a `feature_engineering[].transformation` step on its named input columns.

    Pattern B, the form's own term for it: each `source_columns` key is the name of a
    function PARAMETER and its value names a dataset COLUMN, so `{numerator: rx_count}`
    calls `func(numerator=frame[["rx_count"]])`. Each input arrives as a single-column
    DataFrame alongside `params`, and one single-column DataFrame comes back — the
    column the step writes into the dataset.

    `params` is splatted rather than inspected, the same as the other two call forms:
    stage 1 already bound every key against the real signature, so this is the backstop
    for a form that changed after validation.
    """
    func = resolve(block, field)
    params = _params(block, field)
    source_columns = block.get("source_columns") or {}
    if not isinstance(source_columns, dict) or not source_columns:
        raise SystemExit(
            f"[fail] {field}.source_columns must be a non-empty mapping of "
            f"function-argument to dataset column."
        )

    arguments = {}
    for argument, column in source_columns.items():
        if column not in frame.columns:
            raise SystemExit(
                f"[fail] {field}.source_columns.{argument} names column {column!r}, "
                f"which the dataset does not carry. It has {sorted(frame.columns)}."
            )
        arguments[argument] = frame[[column]]

    overlap = sorted(set(arguments) & set(params))
    if overlap:
        raise SystemExit(
            f"[fail] {field} declares {overlap} in both source_columns and params, so "
            f"the call would bind the same argument twice."
        )

    try:
        result = func(**arguments, **params)
    except TypeError as exc:
        raise SystemExit(
            f"[fail] {field} could not be called: {exc}. source_columns bind "
            f"{sorted(arguments)} and params keys are {sorted(params)}."
        ) from exc

    # One step declares one output column (the template's "one entry per output
    # column"), so a step that returned several has no single column to write back
    # under and the dataset would silently take whichever pandas picked.
    if getattr(result, "shape", (0, 0))[1] != 1:
        raise SystemExit(
            f"[fail] {field} ({describe(block)}) returned "
            f"{getattr(result, 'shape', '?')} — a feature_engineering step writes "
            f"exactly one column, the one its `name` declares."
        )
    return result


def call_column(block, field, column):
    """Call a join-key `transformations[]` step on one join column.

    The column form of the contract: no `source_columns`, because the single input is
    always that side's join column, bound to the function's `column_name` argument
    (handoff §4.1). `column` is a single-column DataFrame and the step returns one,
    which the caller writes back over the join column FOR THAT JOIN ONLY — nothing is
    written back to the dataset or the feature group.

    `params` is splatted rather than inspected: the engine's own contract treats it as
    opaque and lets Python's signature binding reject what does not fit, so a
    TypeError here is that check doing its job. Stage 1 catches the same class of
    error before the job is submitted; this is the backstop for a form that changed
    after validation.
    """
    func = resolve(block, field)
    params = _params(block, field)
    try:
        result = func(column_name=column, **params)
    except TypeError as exc:
        raise SystemExit(
            f"[fail] {field} could not be called: {exc}. params keys are "
            f"{sorted(params)}; the single input column is bound to 'column_name'."
        ) from exc

    # A step that widened or emptied its input has no single column to write back
    # over the join column, so the join would silently run on the wrong values.
    if getattr(result, "shape", (0, 0))[1] != 1:
        raise SystemExit(
            f"[fail] {field} ({describe(block)}) returned "
            f"{getattr(result, 'shape', '?')} — a join-key transformation must return "
            f"exactly one column, which replaces the join column for this join."
        )
    return result


def describe(block):
    """'module.function', for logging and the report.

    Derived from the same two fields that are dispatched on, so the name recorded
    cannot drift from what was actually called.
    """
    return f"{block.get('module', '?')}.{block.get('function', '?')}"
