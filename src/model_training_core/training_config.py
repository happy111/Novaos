"""training_config.py — parse model-training-config.yaml into a typed object.

The single place the training YAML is read on the launcher side (the analogue of
the feature-store sample's intake.py). Exposes ONLY the fields the train+register
pipeline consumes; deferred sections (tuning, retraining, schedule, explainability,
compare_to_champion) are absent from the config entirely and so are not read here.
Model-specific values come from here; environment/infra comes from config.py.

Shape: this reads the CURRENT template only — `identity` for who/what, `data.source`
for the dataset, a `preprocessing` LIST of transform steps, per-step `compute`
blocks, and `storage` / `registry`. Anything the template does not author is not
read: there is no top-level model_name/owner/team, no dataset name (the dataset is
named after the model), no compute.training, and no algorithm.container — per-step
image refs are DERIVED from the model identity (naming.step_image_ref).

Pure by construction: importing this module and calling load() touch no AWS and
need no environment, so the whole layer is unit-testable offline. Business/AWS
checks live in validation.validate_business().
"""

import json
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import naming, validation

# Launchers baked into each step's image. The config does not author these: the
# container owns its own code and we only name which launcher to run, so the name
# is a convention between this module and the images, not a DS knob.
_PREP_ENTRYPOINT = "preprocess_launcher.sh"
_EVAL_ENTRYPOINT = "eval_launcher.sh"


@dataclass
class Compute:
    """One step's hardware + image recipe, from compute.<step> (+ compute.defaults).

    instance_type is the only required knob. The dockerfile / local_requirements
    fields are the step's IMAGE recipe: recorded and carried through, but nothing
    here builds it — see naming.step_image_ref.

    max_runtime_seconds / volume_size_gb / use_spot / checkpoint_s3_uri are NOT
    authored by the current template. They stay here as defaults because the
    pipeline must pass a runtime cap, a volume size and a spot flag to SageMaker
    regardless; when the template authors them, they are read like any other field.
    """

    instance_type: str
    instance_count: int = 1
    max_runtime_seconds: int = 14400
    dockerfile: str = ""
    local_requirements: str = ""
    volume_size_gb: int = 50
    use_spot: bool = False
    checkpoint_s3_uri: str | None = None


@dataclass
class PreprocessingStep:
    """One transform in the ordered preprocessing list.

    Names a CLASS implementing the scikit-learn transformer protocol — `function`
    inside `module` (e.g. StandardScaler in sklearn.preprocessing) — the columns it
    runs on, and the constructor arguments. The prep container instantiates it with
    params, fits it ONCE on the train partition's source_columns, then applies that
    same fitted instance to every partition. The DS composes a pipeline by listing
    steps; there is no fixed vocabulary of transforms in this code.

    source_columns is a FLAT list: a transformer takes one multi-column X, not
    several named single-column arguments.
    """

    module: str
    function: str
    source_columns: list[str]
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        """'sklearn.preprocessing.StandardScaler' — what the prep container imports."""
        return f"{self.module}.{self.function}"

    def to_dict(self) -> dict[str, Any]:
        """Wire form handed to the prep container (see Preprocessing.steps_json)."""
        return {
            "module": self.module,
            "function": self.function,
            "source_columns": list(self.source_columns),
            "params": dict(self.params),
        }


@dataclass
class Preprocessing:
    """The prep step's config (M5 `prep`): an ORDERED list of transforms.

    `steps` is applied sequentially, in YAML order — each step's output feeds the
    next — and is fitted on TRAIN only, then applied to every split. This replaced
    the single `scaler` knob: the transform pipeline is now data authored by the
    DS, not a fixed choice in this code, so the prep container receives the whole
    list (steps_json) and does the importing.

    The image comes from naming.step_image_ref(model_name, "preprocess") and bakes in
    the prep code; prep_entrypoint is the launcher inside it that we run directly
    (mirrors Evaluation.eval_entrypoint). Present only when the YAML carries a
    non-empty `preprocessing` list.
    """

    steps: list[PreprocessingStep]
    compute: Compute
    image_ref: str
    prep_entrypoint: str

    def steps_json(self) -> str:
        """The step list as one compact JSON argument for the prep container.

        Steps carry nested params of arbitrary type, so they cannot go as flat
        CLI tokens the way column lists do. One JSON string keeps ordering and
        types intact. SageMaker caps a single ContainerArgument (and a single
        environment value) at 256 characters and one real step list already
        exceeds that, so the pipeline PUBLISHES this string as an S3 object and
        mounts it into the prep container rather than passing it inline.
        """
        return json.dumps([s.to_dict() for s in self.steps], separators=(",", ":"))

    def source_columns(self) -> list[str]:
        """Every column any step touches, de-duplicated, in first-seen order."""
        seen: list[str] = []
        for step in self.steps:
            for col in step.source_columns:
                if col not in seen:
                    seen.append(col)
        return seen


@dataclass
class MetricSpec:
    """One gate metric declared in the YAML: a name + its threshold + direction.

    Record-only — the verdict is reported, never used to halt the pipeline or
    block registration (automated gating is not implemented yet). The `metric`
    name is a CONTRACT with the evaluate container: it must be a key the container
    writes into metrics.json (the registry step JsonGets it by that exact name)."""

    metric: str
    threshold: float = 0.0
    threshold_direction: str = (
        "greater_than"  # greater_than (auc/f1) | less_than (rmse/loss)
    )

    def passes(self, value: float) -> bool:
        """Report-only verdict for this metric vs. its configured threshold."""
        if self.threshold_direction == "less_than":
            return value <= self.threshold
        return value >= self.threshold


@dataclass
class BiasConfig:
    """SageMaker Clarify bias settings (evaluation.bias_config).

    One Clarify job covers two metric families: PRE-training measures skew in the
    data (label only, no model), POST-training measures skew in the model
    (prediction vs ground truth). group_name is the single confounder column that
    enables the conditional metrics — CDDL (pre) and CDDPL (post) — and both
    return null without it.

    Column names here must survive preprocessing into the scored test split;
    that check needs the dataset and so lives in validate_business().
    """

    label_column: str = ""
    label_values_or_threshold: list[Any] = field(default_factory=list)
    facet_columns: list[str] = field(default_factory=list)
    facet_values_or_threshold: list[Any] = field(default_factory=list)
    group_name: str = ""
    predicted_label_column: str = "predicted_label"
    pre_training_metrics: str | list[str] = "all"
    post_training_metrics: str | list[str] = "all"


@dataclass
class Evaluation:
    """The evaluate step's config. Record-only: thresholds are reported, never
    used to halt the pipeline or block registration (automated gating is not
    implemented yet). eval_entrypoint is the launcher baked into the evaluate image
    that the evaluate ProcessingStep runs.

    primary_metrics are the DS-declared gate metrics (each name + threshold +
    direction); secondary_metrics are additional names logged but not gated. Both
    metric NAMES flow into the registered model card (see card_metric_names) — this
    is what replaced the pipeline's old hardcoded metric-key list: the card now
    shows exactly the metrics the YAML lists, not a fixed set baked into the code."""

    primary_metrics: list[MetricSpec]
    eval_entrypoint: str  # launcher inside the eval image (e.g. eval_launcher.sh)
    compute: Compute
    image_ref: str  # account-free repo:tag for the evaluate container
    secondary_metrics: list[str] = field(default_factory=list)
    source_script: str = ""
    run_bias_check: bool = False
    bias: BiasConfig = field(default_factory=BiasConfig)

    @property
    def bias_label_column(self) -> str:
        """Ground-truth column Clarify reads. Resolved at load() time."""
        return self.bias.label_column

    @property
    def bias_facet_columns(self) -> list[str]:
        return list(self.bias.facet_columns)

    @property
    def bias_predicted_label_column(self) -> str:
        """Prediction column Clarify reads for the POST-training metrics.

        A CONTRACT with the evaluate container: it must be the header that container
        writes into predictions.csv. Authorable because a model can ship its own
        evaluate image; blank falls back to the platform image's name at load()."""
        return self.bias.predicted_label_column

    def bias_facet_values(self) -> list[list[Any]] | None:
        """facet_values_or_threshold reshaped for Clarify's BiasConfig.

        The YAML authors ONE entry per facet column, in the same order, because that
        is the common case (one sensitive value per attribute). Clarify wants a list
        PER facet — it reads element i as facet i's value_or_threshold, and that
        slot is itself a list for categorical facets — so each authored scalar is
        wrapped. An entry already written as a list passes through, which is how a
        facet with several sensitive categories is expressed.

        None means the field was left blank, which tells Clarify to report every
        value of every facet rather than a chosen sensitive group.

        Raises ValueError on a length mismatch: Clarify would raise the same thing
        with a less specific message, and doing it here keeps the failure at build
        time rather than in the job."""
        values = self.bias.facet_values_or_threshold
        if not values:
            return None
        facets = self.bias.facet_columns
        if len(values) != len(facets):
            raise ValueError(
                "evaluation.bias_config.facet_values_or_threshold has "
                f"{len(values)} entry/entries but facet_columns has {len(facets)} "
                f"({facets}). Author one value per facet column, in the same order, "
                "or leave facet_values_or_threshold blank to report every value."
            )
        return [v if isinstance(v, list) else [v] for v in values]

    def card_metric_names(self) -> list[str]:
        """Metric names the registry step pulls from metrics.json into the model card.

        Primary (gate) metrics first, then any secondary metrics not already named,
        preserving YAML order and dropping duplicates. Each name MUST be a key the
        evaluate container writes into metrics.json — a missing JsonGet path fails
        RegisterModel — so the DS lists only metrics the container actually emits."""
        names: list[str] = []
        for m in self.primary_metrics:
            if m.metric and m.metric not in names:
                names.append(m.metric)
        for name in self.secondary_metrics:
            if name and name not in names:
                names.append(name)
        return names


@dataclass
class Registry:
    """Registry behaviour (M6) — the part the template defers.

    The Model Package Group is NOT authored: it is derived from model_name through
    naming.model_package_group(), which also applies the slug SageMaker requires.

    register_on_fail is carried, not enforced. There is no ConditionStep between
    evaluate and register, so every run registers a version whatever the gate says
    — which is what `true` describes. The field exists so a config can state the
    intent now and have it honoured when gating lands, and so the value does not
    silently mean its opposite in the meantime.
    """

    register_on_fail: bool = False


@dataclass
class Storage:
    """Where this model's run artifacts land. Blank = the platform artifact prefix."""

    s3_prefix: str = ""


@dataclass
class TrainingConfig:
    """The contract between the config layer and the pipeline builder.

    Everything the pipeline needs from the YAML is on this object; the builder
    never reads the YAML itself. Field names here are the pipeline-facing contract
    and do not always match the YAML key they came from (data.target_columns ->
    target_variables, storage -> storage but s3_prefix distinct from the dataset's)
    — absorbing template renames is exactly this module's job.
    """

    model_name: str
    owners: list[str]
    business_unit: str
    description: str
    target_variables: list[str]
    dataset_name: str  # dataset folder name (= model_name)
    dataset_version: str  # 'latest' or a pinned version
    image_ref: str  # account-free repo:tag for the training image
    hyperparameters: dict[str, Any]
    compute: Compute  # the TRAIN step's compute
    algorithm_type: str = "custom"
    source_script: str = ""
    random_seed: int | None = None
    model_version: str = "auto"
    dataset_s3_prefix: str = "mrd"
    feature_columns: list[str] = field(default_factory=list)
    drop_columns: list[str] = field(default_factory=list)
    drop_zero_variance: bool = False
    preprocessing: Preprocessing | None = None
    evaluation: Evaluation | None = None
    inference: Compute | None = None  # compute.inference — serving, not a step
    storage: Storage = field(default_factory=Storage)
    registry: Registry = field(default_factory=Registry)
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def owner(self) -> str:
        """Primary owner. identity.owners is a list; the first entry is the DS of record."""
        return self.owners[0] if self.owners else ""

    @property
    def target_variable(self) -> str:
        """The single target column we train on.

        data.target_columns is authored as a LIST (multi-target is not supported
        yet), but every downstream step — the container hyperparameter, the
        prep argument, the Clarify label — takes exactly one column, so the
        first entry is authoritative.
        """
        return self.target_variables[0] if self.target_variables else ""

    def all_tags(self) -> dict[str, str]:
        """Cost/tracking tags = base tags + identity-derived tags + YAML tags."""
        from . import config

        merged = dict(config.BASE_TAGS)
        merged.update(
            {
                "model_name": self.model_name,
                "owner": self.owner,
                "business_unit": self.business_unit,
            }
        )
        merged.update(self.tags or {})
        return merged


def _target_variables(data):
    """data.target_columns, blanks dropped. Raises if none remain."""
    target_variables = [t for t in (data.get("target_columns") or []) if t]
    if not target_variables:
        raise ValueError(
            "data.target_columns is required and must name at least one column."
        )
    return target_variables


def _dataset_fields(data, model_name):
    """dataset_name/version/s3_prefix for TrainingConfig, from data.source.

    The config authors no dataset name: the labeled dataset produced for this
    model by model-ready-dataset-intake-form.yaml is named after the model.
    dataset_name stays the model name AS WRITTEN — naming.dataset_dir_name() is
    what slugs it at the actual S3-lookup call site (datasets.resolve_dataset),
    so this field can stay the identity, not the lookup path.
    """
    source = data.get("source") or {}
    return {
        "dataset_name": model_name,
        "dataset_version": (source.get("dataset_version") or "").strip() or "latest",
        "dataset_s3_prefix": source.get("s3_prefix", "") or "mrd",
    }


def _algorithm_fields(algorithm):
    """algorithm_type/source_script/random_seed/hyperparameters for TrainingConfig."""
    return {
        "algorithm_type": algorithm.get("type", "custom"),
        "source_script": algorithm.get("source_script", "") or "",
        "random_seed": (
            int(algorithm["random_seed"])
            if algorithm.get("random_seed") is not None
            else None
        ),
        "hyperparameters": algorithm.get("hyperparameters") or {},
    }


def _model_version(raw):
    """The DS's own version for the config document, from metadata.version.

    Stays the "auto" sentinel when unauthored: SageMaker assigns the registered
    version at register time, so there is no version to state here, and
    consumers must not print the sentinel as if it were one.
    """
    return str((raw.get("metadata") or {}).get("version") or "").strip() or "auto"


def load(path, *, validate_schema=True) -> TrainingConfig:
    """Read + (optionally) structurally validate the YAML, return a TrainingConfig.

    Business/AWS validation (dataset exists, image resolvable) is intentionally
    NOT done here — the launcher calls validation.validate_business() when it has
    live AWS context. Keeping load() free of AWS and of environment reads makes it
    unit-testable offline.
    """
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    if validate_schema:
        validation.validate_structure(raw)

    identity = raw.get("identity") or {}
    model_name = naming.validate_model_name(identity.get("model_name"))

    data = raw.get("data") or {}
    algorithm = raw.get("algorithm") or {}
    target_variables = _target_variables(data)

    train_compute = _compute(raw, "train")
    dataset_fields = _dataset_fields(data, model_name)
    algorithm_fields = _algorithm_fields(algorithm)
    return TrainingConfig(
        model_name=model_name,
        owners=list(identity.get("owners") or []),
        business_unit=identity.get("business_unit", "") or "",
        description=(identity.get("description") or "").strip(),
        target_variables=target_variables,
        dataset_name=dataset_fields["dataset_name"],
        dataset_version=dataset_fields["dataset_version"],
        dataset_s3_prefix=dataset_fields["dataset_s3_prefix"],
        image_ref=naming.step_image_ref(
            model_name, "train", train_compute.instance_type
        ),
        algorithm_type=algorithm_fields["algorithm_type"],
        source_script=algorithm_fields["source_script"],
        random_seed=algorithm_fields["random_seed"],
        model_version=_model_version(raw),
        hyperparameters=algorithm_fields["hyperparameters"],
        compute=train_compute,
        feature_columns=list(data.get("feature_columns") or []),
        drop_columns=list(data.get("drop_columns") or []),
        drop_zero_variance=bool(data.get("drop_zero_variance", False)),
        preprocessing=_preprocessing(raw, model_name),
        evaluation=_evaluation(raw, model_name, target_variables),
        inference=_inference(raw),
        storage=_storage(raw),
        registry=_registry(raw),
        metadata=raw.get("metadata") or {},
        tags=raw.get("tags") or {},
    )


def _compute(raw, step) -> Compute:
    """Build a Compute for one step from compute.<step>, over compute.defaults.

    The template authors a block per step (preprocess/train/evaluate) and no
    shared base, but an optional `defaults` block is still honoured: it applies to
    every step and the per-step block overrides only the fields it names, so an
    absent per-step block means "use defaults verbatim".

    instance_type must survive the merge. The schema cannot express "present on the
    step OR on defaults" with a usable error message, so this is the check that
    enforces it — and its message names both places the DS can set it.
    """
    compute = raw.get("compute") or {}
    merged = dict(compute.get("defaults") or {})
    for key, value in (compute.get(step) or {}).items():
        # A per-step key present but blank means "not overridden" — the template
        # ships blank placeholders for the optional fields.
        if value not in ("", None):
            merged[key] = value

    instance_type = merged.get("instance_type")
    if not instance_type:
        raise ValueError(
            f"compute.{step}.instance_type is required (set it on compute.{step} "
            f"or on compute.defaults)."
        )
    return Compute(
        instance_type=instance_type,
        instance_count=int(merged.get("instance_count", 1)),
        dockerfile=merged.get("dockerfile", "") or "",
        local_requirements=merged.get("local_requirements", "") or "",
    )


def _preprocessing(raw, model_name) -> Preprocessing | None:
    """Parse the ordered preprocessing list from preprocessing[] + compute.preprocess.

    Returns None only when the config carries NO `preprocessing` key at all — the
    train-only flow, feeding raw parquet straight to the container.

    An explicit empty list is NOT the same thing, and collapsing the two was a
    bug. `preprocessing: []` asks for the prep step on its IDENTITY path, which
    still writes X_*/y_* per split, still carries the facet columns into
    split_test.parquet, and still writes preprocess_meta.json (with `steps: []`);
    only the fitted scaler.pkl is skipped, there being nothing fitted. Those
    artifacts are the ModelTrainer/ModelEvaluator entry-point contract — the
    scripts rebuild named frames from output_columns/target_variable — so a model
    that needs no fitted transform still needs the step that produces them. Read
    as None, such a model got raw parquet channels and died looking for a meta
    file, and its only way out was to author a transform it did not want.

    A bare `preprocessing:` (null) never reaches here: the schema types the field
    as an array, so null is rejected before parsing.

    Steps are kept in YAML order: the prep container applies them sequentially, so
    order is semantic — a scaler after an imputer is not the same pipeline as the
    reverse.
    """
    steps_raw = raw.get("preprocessing")
    if steps_raw is None:
        return None
    steps = [
        PreprocessingStep(
            module=s["module"],
            function=s["function"],
            source_columns=list(s.get("source_columns") or []),
            params=s.get("params") or {},
        )
        for s in steps_raw
    ]

    compute = _compute(raw, "preprocess")
    return Preprocessing(
        steps=steps,
        compute=compute,
        image_ref=naming.step_image_ref(
            model_name, "preprocess", compute.instance_type
        ),
        prep_entrypoint=_PREP_ENTRYPOINT,
    )


def _evaluation(raw, model_name, target_variables) -> Evaluation | None:
    """Parse the evaluate step from evaluation.* + compute.evaluate.

    Returns None when the config carries no `evaluation` block, so train-only
    configs still load. The evaluate machine comes from compute.evaluate (its OWN
    knob); the evaluate image is derived from the model identity.
    """
    eval_raw = raw.get("evaluation")
    if not eval_raw:
        return None

    compute = _compute(raw, "evaluate")
    return Evaluation(
        primary_metrics=_primary_metrics(eval_raw),
        secondary_metrics=[m for m in (eval_raw.get("secondary_metrics") or []) if m],
        source_script=eval_raw.get("source_script", "") or "",
        eval_entrypoint=_EVAL_ENTRYPOINT,
        compute=compute,
        image_ref=naming.step_image_ref(model_name, "evaluate", compute.instance_type),
        run_bias_check=bool(eval_raw.get("run_bias_check", False)),
        bias=_bias_config(eval_raw, target_variables),
    )


def _bias_config(eval_raw, target_variables) -> BiasConfig:
    """Parse evaluation.bias_config, resolving the label column default.

    A blank label_column falls back to the primary target variable, as the
    template documents. That default is safe to rely on: the evaluate step passes
    the resolved name to the container (--bias-label-column), so predictions.csv
    writes its ground-truth column under whatever this returns rather than under a
    fixed name of its own.

    Blank and ABSENT resolve identically in every field here, and that is the half
    of the contract the schema leans on: the schema accepts "" / [] wherever a field
    documents a default — which is what lets run_bias_check: false leave the
    template's stub in place — and it never substitutes anything, so resolving blank
    has to happen HERE. Hence `or "all"` on the metric selections rather than a
    get() default: get() fires only when the key is missing, so an authored [] would
    reach Clarify as "compute no metrics" instead of as "unset".
    """
    bias = eval_raw.get("bias_config") or {}
    default_label = target_variables[0] if target_variables else ""
    return BiasConfig(
        label_column=(bias.get("label_column") or "").strip() or default_label,
        label_values_or_threshold=list(bias.get("label_values_or_threshold") or []),
        facet_columns=[c for c in (bias.get("facet_columns") or []) if c],
        facet_values_or_threshold=list(bias.get("facet_values_or_threshold") or []),
        group_name=(bias.get("group_name") or "").strip(),
        predicted_label_column=(bias.get("predicted_label_column") or "").strip()
        or "predicted_label",
        pre_training_metrics=bias.get("pre_training_metrics") or "all",
        post_training_metrics=bias.get("post_training_metrics") or "all",
    )


def _primary_metrics(eval_raw) -> list[MetricSpec]:
    """Parse evaluation.primary_metric into a list of MetricSpec (gate metrics).

    primary_metric is a LIST of {metric, threshold, threshold_direction} objects
    (the DS may gate on more than one held-out metric). The first entry is the
    principal gate; the rest are additional gates. Each `metric` is required and
    non-empty (it is the metrics.json key the registry step reads); threshold and
    threshold_direction default when omitted.
    """
    raw_metrics = eval_raw.get("primary_metric")
    if not raw_metrics:
        raise ValueError(
            "evaluation.primary_metric is required and must be a non-empty list of "
            "{metric, threshold, threshold_direction} entries."
        )
    specs: list[MetricSpec] = []
    for i, entry in enumerate(raw_metrics):
        name = (entry.get("metric") or "").strip()
        if not name:
            raise ValueError(
                f"evaluation.primary_metric[{i}].metric is required and must be non-empty."
            )
        specs.append(
            MetricSpec(
                metric=name,
                threshold=float(entry.get("threshold", 0.0)),
                threshold_direction=entry.get("threshold_direction", "greater_than"),
            )
        )
    return specs


def _storage(raw) -> Storage:
    storage = raw.get("storage") or {}
    return Storage(s3_prefix=storage.get("s3_prefix", "") or "")


def _registry(raw) -> Registry:
    """Parse the registry block. Every field in it is deferred, so nothing here
    can fail: an absent block and a blank one are the same config."""
    reg = raw.get("registry") or {}
    return Registry(register_on_fail=bool(reg.get("register_on_fail", False)))


def _inference(raw) -> Compute | None:
    """compute.inference — the SERVING recipe, or None when not authored.

    Same four fields as any other compute block, so it reuses _compute: the
    template moved the serving container in beside the step blocks. It is not a
    step, though — no inference job runs in this pipeline. Its instance_type is
    recorded on the registered version as the recommended inference/transform
    instance, and its dockerfile marks a model that ships its OWN serving image
    rather than being served by the generic platform one.

    Returns None when the section is absent: a train-only config authors no
    serving hardware, and the register step then falls back to the train
    instance type (which is what it used before this block existed).
    """
    if "inference" not in (raw.get("compute") or {}):
        return None
    return _compute(raw, "inference")
