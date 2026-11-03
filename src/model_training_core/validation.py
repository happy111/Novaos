"""validation.py — validate model-training-config.yaml before the pipeline runs.

Three layers, per the repo's structure rules:
  1. STRUCTURAL — validate the parsed YAML against schemas/model-training.schema.json
     (required fields, types, enums). Pure, no AWS calls.
  2. CROSS-FILE — rules spanning two committed YAMLs, which no single file's schema
     can state: the training gate vs the approved success criteria in
     model-onboarding-form.yaml. Also pure — both files are inputs.
  3. CROSS-FIELD + BUSINESS/AWS — rules relating two fields of the SAME file, which
     the schema cannot state because it validates each field in isolation, plus the
     checks needing live context: the target dataset exists in S3, the ECR image is
     resolvable. Entered through validate_business(), pure rules first.

Structural failures hard-fail with a clear message listing the offending path.
Layers 1 and 2 run before any AWS call, so a bad config costs no compute.
"""

import json
import os

import jsonschema

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "schemas",
    "model-training.schema.json",
)


def load_schema(path=_SCHEMA_PATH):
    with open(path) as f:
        return json.load(f)


def validate_structure(cfg, schema=None):
    """Validate the parsed config dict against the JSON Schema.

    Raises ValueError with the full instance path on the first failure so the DS
    knows exactly which field to fix.
    """
    schema = schema or load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(cfg), key=lambda e: list(e.absolute_path))
    if errors:
        lines = []
        for e in errors:
            loc = "/".join(str(p) for p in e.absolute_path) or "(root)"
            lines.append(f"  - {loc}: {e.message}")
        raise ValueError(
            "model-training-config.yaml failed schema validation:\n" + "\n".join(lines)
        )


_DEFAULT_CONFIG_NAME = "model-training-config.yaml"
_DEFAULT_FORM_NAME = "model-onboarding-form.yaml"


def _is_looser(threshold, direction, approved):
    """Is `threshold` a weaker bar than `approved` for this metric direction?

    greater_than metrics (auc, f1) get weaker as the threshold DROPS; less_than
    metrics (rmse, loss) get weaker as it RISES.
    """
    if direction == "less_than":
        return threshold > approved
    return threshold < approved


def validate_kpi_gate(cfg, onboarding, *, config_path=None):
    """Cross-file rule: the training gate must not be looser than the approved one.

    The onboarding form is signed off by the Business Sponsor, and its
    success_criteria names the metric and the minimum value the model has to reach
    to be approved. The training config declares its own registration gate
    independently, so nothing stops a DS from authoring a lower threshold — the run
    would pass its own gate, register a version, and only fail review later, after
    the compute is spent. This rejects the mismatch up front.

    Two mismatches are refused:
      - the approved metric is not gated by the config at all (including a config
        that only lists it under secondary_metrics, which is logged, not gated);
      - the config gates it, but at a looser threshold than the approved minimum.

    Metric names are compared case-insensitively: the form is written for humans
    ("AUC") and the config for the evaluate container ("auc"). No-ops when either
    side declares no gate — the form is an optional input and ships its
    success_criteria blank.

    Pure: no AWS, no filesystem. Raises ValueError naming both values and both
    source files, because the caller does not retry and the DS gets one message.
    """
    if onboarding is None or not onboarding.success_metric:
        return
    approved = onboarding.minimum_threshold
    if approved is None:
        return

    form_path = onboarding.source_path or _DEFAULT_FORM_NAME
    config_path = config_path or _DEFAULT_CONFIG_NAME
    wanted = onboarding.success_metric.lower()

    gates = list(cfg.evaluation.primary_metrics) if cfg.evaluation else []
    secondary = list(cfg.evaluation.secondary_metrics) if cfg.evaluation else []
    match = next(
        ((i, g) for i, g in enumerate(gates) if g.metric.strip().lower() == wanted),
        None,
    )

    if match is None:
        declared = ", ".join(g.metric for g in gates) or "(none)"
        hint = ""
        if any(m.strip().lower() == wanted for m in secondary):
            hint = (
                f"\n'{onboarding.success_metric}' is listed under "
                "evaluation.secondary_metrics, which is logged but not gated."
            )
        raise ValueError(
            f"the approved success metric is not gated by the training config:\n"
            f"  - success_criteria.primary_metric = '{onboarding.success_metric}' "
            f"(minimum_threshold {approved})   [{form_path}]\n"
            f"  - evaluation.primary_metric gates: {declared}   [{config_path}]\n"
            f"Add an evaluation.primary_metric entry for "
            f"'{onboarding.success_metric}', or have the Business Sponsor revise "
            f"the onboarding form.{hint}"
        )

    index, gate = match
    if _is_looser(gate.threshold, gate.threshold_direction, approved):
        comparison = "<=" if gate.threshold_direction == "less_than" else ">="
        raise ValueError(
            f"the training gate on '{gate.metric}' is looser than the approved "
            f"success criteria:\n"
            f"  - evaluation.primary_metric[{index}].threshold = {gate.threshold} "
            f"({gate.threshold_direction})   [{config_path}]\n"
            f"  - success_criteria.minimum_threshold = {approved}   [{form_path}]\n"
            f"A model passing this gate could not be approved: '{gate.metric}' must "
            f"be {comparison} {approved}. Tighten the training threshold, or have "
            f"the Business Sponsor revise the onboarding form."
        )


def validate_bias_config(cfg, *, config_path=None):
    """Cross-FIELD bias rules, which no single field's schema can state.

    The schema checks each bias field in isolation — it requires
    label_values_or_threshold and facet_columns to be non-empty whenever
    run_bias_check is true, and that covers those two on its own. What it cannot
    express is a rule RELATING two sibling arrays: facet_values_or_threshold is
    positional, one entry per facet column, so authoring two facets and one value
    silently pairs the wrong value with the wrong attribute. JSON Schema has no way
    to compare the lengths of two arrays.

    The rule itself lives on Evaluation.bias_facet_values(), which is where the
    reshaping happens and therefore where the message belongs; this only makes it
    run at validate time instead of at pipeline build. Without it, a mismatch passes
    State 1 and fails one state later, after the config was already accepted.

    Pure: no AWS, no filesystem. No-ops when the config asks for no bias check.
    """
    if cfg.evaluation is None or not cfg.evaluation.run_bias_check:
        return
    try:
        cfg.evaluation.bias_facet_values()
    except ValueError as exc:
        raise ValueError(f"{exc}   [{config_path or _DEFAULT_CONFIG_NAME}]") from None


def validate_business(cfg, s3_client=None, *, config_path=None):
    """Checks the JSON Schema cannot express. Takes a parsed TrainingConfig.

    Currently: the cross-field bias rules, then the resolved dataset name must point
    at a valid (manifest-carrying) version in S3, with at least a train split.
    (algorithm.type == 'custom' is already enforced structurally by the schema's
    const.)

    The pure check runs FIRST so a config that fails it costs no S3 call — the same
    ordering rationale that puts validate_kpi_gate ahead of this function.

    Returns the resolved channels map (split -> {s3_uri, version, split}).
    """
    validate_bias_config(cfg, config_path=config_path)

    # Import here to avoid a hard boto3 dependency for pure structural validation.
    from . import datasets

    return datasets.resolve_channels(
        {
            "dataset_name": cfg.dataset_name,
            "dataset_version": cfg.dataset_version,
            "dataset_s3_prefix": cfg.dataset_s3_prefix,
        },
        s3_client=s3_client,
    )
