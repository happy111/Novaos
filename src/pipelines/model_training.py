"""model_training.py — the reusable AR model-training pipeline builder (M5 MVP).

Assembles a SageMaker Pipeline from a validated TrainingConfig. The full chain,
with the three middle stages appearing only when the YAML enables them:

    0. ProcessingStep  — (optional) preprocessing (M5 prep). Runs on the dedicated
                         prep image for this model, invoking its BAKED-IN launcher.
                         Applies the YAML's ORDERED list of transform steps, fitted
                         on TRAIN only, and emits the .npy arrays + fitted
                         transformers + split_test.parquet the BYO container expects.
                         Present only when the YAML lists preprocessing steps; when
                         absent, the raw M4 parquet splits feed the container directly.
    1. TrainingStep    — run the BYO container (Path A). Reads prep's output when
                         present (a single 'train' channel), else the pre-split M4
                         dataset (train/validation/holdout channels); writes
                         model.tar.gz to S3.
    2. ProcessingStep  — (optional, AR-08-b) score the trained artifact on the
                         holdout, on the dedicated evaluate image invoking its
                         baked-in eval launcher. RECORD-ONLY: writes
                         metrics.json/predictions.csv but NEVER gates registration
                         (automated gating is deferred to SOW 5.6.1), so there is no
                         ConditionStep — Register only depends_on it for ordering.
                         Present only when the YAML carries an evaluation block.
    3. ProcessingStep  — (optional, AR-08-b) SageMaker Clarify post-training bias
                         check on predictions.csv (AWS-managed image, no model
                         invocation/endpoint). Also RECORD-ONLY. Present only when
                         the evaluation block sets run_bias_check with a facet.
    4. ModelStep       — register the trained artifact into a Model Package Group
                         with PendingManualApproval. Attaches the Model Card
                         (AR-08-c, from the onboarding form) and ModelMetrics
                         (pointing at the evaluate/bias evidence) when available.

Tuning and scheduling are NOT supported yet (see
docs/lld/yaml-templates/model-training-config.yaml). This builder is GENERIC: it holds no model
names, SQL, or model-specific branches — everything comes from the YAML (model
config) and src/model_training_core/config.py (environment/infra). A new model = a new YAML,
no code change.

SageMaker Python SDK v3 (sagemaker.core / sagemaker.mlops / sagemaker.train).
The v3 pipeline pattern: build the job object under a PipelineSession so calling
.train() / .register() CAPTURES the request as `step_args` instead of running it;
the captured value is handed to the *Step. The Pipeline is serialized to JSON and
upserted once; each run is a StartPipelineExecution with parameters.
"""

import hashlib
import json

from sagemaker.core import Processor
from sagemaker.core.clarify import (
    BiasConfig,
    DataConfig,
    SageMakerClarifyProcessor,
)
from sagemaker.core.model_metrics import MetricsSource, ModelMetrics

# TWO different ModelPackageModelCard classes ship in v3: the one in
# sagemaker.core.model_card is really ModelCardContent (the section shapes);
# builder.register() checks isinstance against sagemaker.core.resources'
# ModelPackageModelCard, which wraps the content as a JSON STRING + a status.
# So we assemble ModelCardContent, serialize it, and hand register() the resources card.
from sagemaker.core.resources import ModelPackageModelCard
from sagemaker.core.shapes import (
    Channel,
    DataSource,
    OutputDataConfig,
    ProcessingInput,
    ProcessingOutput,
    ProcessingS3Input,
    ProcessingS3Output,
    S3DataSource,
    StoppingCondition,
)
from sagemaker.core.shapes.model_card_shapes import (
    AdditionalInformation,
    BusinessDetails,
    IntendedUses,
    ModelCardContent,
    ModelOverview,
)
from sagemaker.core.training.configs import Compute
from sagemaker.core.workflow.execution_variables import ExecutionVariables
from sagemaker.core.workflow.functions import Join, JsonGet
from sagemaker.core.workflow.parameters import ParameterString
from sagemaker.core.workflow.pipeline_context import PipelineSession
from sagemaker.core.workflow.properties import PropertyFile
from sagemaker.mlops.workflow.model_step import ModelStep
from sagemaker.mlops.workflow.pipeline import Pipeline
from sagemaker.mlops.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.serve import ModelBuilder
from sagemaker.train import ModelTrainer

from src.model_training_core import config, naming
from src.model_training_core.onboarding_form import OnboardingForm

# sagemaker v3 eagerly validates the training role by calling iam:SimulatePrincipalPolicy
# while constructing ModelTrainer. IAM has no VPC interface endpoint, so from this
# VPC-bound launch Lambda that call reaches nothing and times out (~90s). This is a
# build-only step — SageMaker validates the role for real when the job runs — so we
# neutralize the pre-flight simulation: report "no actions denied" without touching IAM.
# Pinned to sagemaker==3.15.0; revisit if that internal helper moves on upgrade.
try:
    from sagemaker.core.helper import iam_role_resolver as _iam_role_resolver

    _iam_role_resolver._simulate_denied_actions = lambda *args, **kwargs: []
except (ImportError, AttributeError):  # pragma: no cover - SDK layout changed
    pass


def _channels(resolved_channels):
    """Turn the resolved {split: {s3_uri,...}} map into v3 Channel objects.

    Each split (train, and optionally validation/holdout) is mounted in the
    container at /opt/ml/input/data/<split>/. s3_uri points at that split's
    parquet file; S3Prefix matches the exact object key.
    """
    channels = []
    for name, info in resolved_channels.items():
        channels.append(
            Channel(
                channel_name=name,
                data_source=DataSource(
                    s3_data_source=S3DataSource(
                        s3_data_type="S3Prefix",
                        s3_uri=info["s3_uri"],
                        s3_data_distribution_type="FullyReplicated",
                    )
                ),
            )
        )
    return channels


_JSON_CONTENT_TYPE = "application/json"  # MIME type for the JSON we hand to AWS
_PROGRAM_DIR = "/opt/program"  # where the launchers are baked in the image
_MODEL_IN = "/opt/ml/processing/input/model"
_TEST_IN = "/opt/ml/processing/input/test"
_EVAL_OUT = "/opt/ml/processing/output/evaluation"

# The one content type the Clarify bias check and the registered package's
# inference spec both speak — Clarify reads predictions.csv, and the serving
# image's /invocations default (job-inference-{cpu,gpu}/serving/app.py) is CSV.
_CSV_CONTENT_TYPE = "text/csv"

# PropertyFile name for the Evaluate step's metrics.json — the handle RegisterModel
# JsonGets the held-out numbers from at runtime.
_EVAL_METRICS_PF = "EvalMetrics"

# Build-time placeholder for the card's model_overview.model_artifact. The real
# artifact URI is the Train step's runtime output (S3ModelArtifacts) — a pipeline
# property object that can't be serialized into the card's JSON string. So we
# serialize this sentinel into the card, then split the JSON on it and splice the
# runtime property back in via Join (same trick as the metrics). Chosen to never
# collide with real content and to need no JSON escaping when replaced by an S3 URI.
_ARTIFACT_SENTINEL = "__AR_MODEL_ARTIFACT_URI__"

# The metrics.json keys the card's training_metrics list draws from are NO LONGER
# hardcoded here — they come from the YAML's evaluation block (primary_metric names
# + secondary_metrics), via cfg.evaluation.card_metric_names(). Each name is still a
# CONTRACT with the evaluate container: it must be a key the container writes into
# metrics.json (name == JsonGet path == emitted card name — no rename), and a missing
# path fails RegisterModel. So the DS lists only metrics the container actually emits.
# The canonical names (accuracy/precision/recall/f1) still land in Unified Studio's
# Performance box; anything else (e.g. auc) shows in the Custom box.

# Prep step (M5 prep) container paths. preprocess.py is baked into the dedicated
# prep image and run via its launcher; the raw parquet splits mount at
# _PREP_DATA_IN, output lands at _PREP_OUT.
_PREP_DATA_IN = "/opt/ml/processing/input/data"
_PREP_OUT = "/opt/ml/processing/output/prep"
# The ordered transform list is MOUNTED as a file here, not passed as an argument —
# see _upload_prep_steps for why.
_PREP_STEPS_IN = "/opt/ml/processing/input/steps"


def _run_scoped(run_root, *tail):
    """Namespace a path under the per-model run root by the pipeline EXECUTION ID.

    The processing steps (prep/evaluate/bias) and the training output share one
    per-model root (naming.model_run_root) with NO run identifier, so two executions
    of the same pipeline would overwrite each other's arrays / metrics.json /
    predictions.csv / bias analysis / model.tar.gz. Interposing
    Execution.PipelineExecutionId right after the root — BEFORE the step leaf dir —
    gives every run ONE self-contained folder that holds all of its step outputs:

        <run_root>/<exec-id>/prep/…
        <run_root>/<exec-id>/evaluation/{metrics.json,predictions.csv}
        <run_root>/<exec-id>/bias/{analysis.json,report}
        <run_root>/<exec-id>/output/<training-job>/model.tar.gz

    so concurrent or repeated runs are isolated and each run is a single durable,
    browsable, lifecycle-able unit instead of scattering one run across per-step
    top-level folders.

    Returns a Join pipeline-expression: '<run_root>/<exec-id>[/<tail...>]'. The same
    expression resolves to the same folder across steps (the execution id is
    constant within a run), so a producer and its consumers agree at runtime. Pass
    the step's leaf dir + filename as `tail` (e.g. naming.EVAL_DIR, "metrics.json").
    """
    return Join(
        on="/", values=[run_root, ExecutionVariables.PIPELINE_EXECUTION_ID, *tail]
    )


def _upload_prep_steps(cfg, session, kms_key_arn):
    """Publish the ordered transform list to S3; return (s3 uri, object basename).

    WHY a mounted FILE and not a container argument: SageMaker caps ONE
    ContainerArguments member at 256 characters, and the step list is a single JSON
    document (nested, arbitrarily typed params) that cannot be split across tokens
    the way the column lists are. One StandardScaler over 14 feature columns already
    serializes to ~430 characters, so an inline --steps works for toy configs and
    fails at CreateProcessingJob for real ones. Environment variables are capped at
    the same 256, so they are not an escape hatch either. The container accepts both
    forms and prefers the file (preprocess.py: --steps-file wins over --steps).

    This is a BUILD-time upload — the same thing Clarify already does with its
    analysis_config.json — so it needs no S3 permission the builder does not hold,
    and it is the reason building a pipeline is not an offline operation.

    Encrypted with the platform KMS key when one is configured, matching the
    volume/output encryption on every step.
    """
    payload = cfg.preprocessing.steps_json().encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    key = naming.prep_steps_key(cfg.model_name, digest)
    encryption = (
        {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key_arn}
        if kms_key_arn
        else {}
    )
    session.boto_session.client("s3").put_object(
        Bucket=config.ARTIFACT_BUCKET,
        Key=key,
        Body=payload,
        ContentType=_JSON_CONTENT_TYPE,
        **encryption,
    )
    return f"s3://{config.ARTIFACT_BUCKET}/{key}", key.rsplit("/", 1)[-1]


def _prep_step(cfg, resolved_channels, session, role, kms_key_arn, image_uri, out_uri):
    """Build the preprocessing ProcessingStep (M5 prep).

    Runs on the dedicated prep image derived from the model identity
    (naming.step_image_ref(model_name, "preprocess")). The image bakes in preprocess.py +
    its launcher; we run that launcher (/opt/program/<entrypoint>) directly — the
    container runs its own code, honoring the per-step container contract
    (parallels the evaluate step).
    Inputs: the raw M4 parquet splits (each mounted into one shared data dir), plus
    the ordered transform list as its own mounted JSON object.
    Output: the transformed .npy arrays + fitted transformers + split_test.parquet
    at _PREP_OUT, uploaded to the run-scoped prep prefix and consumed as BOTH the
    Train channel and the Evaluate test input. Feature list / target / facets /
    column filters come from the config as ARGUMENTS and the transform list as a
    mounted file — preprocess.py stays source-agnostic.
    """
    processor = Processor(
        image_uri=image_uri,
        role=role,
        instance_count=cfg.preprocessing.compute.instance_count,
        instance_type=cfg.preprocessing.compute.instance_type,
        volume_size_in_gb=cfg.preprocessing.compute.volume_size_gb,
        volume_kms_key=kms_key_arn or None,
        output_kms_key=kms_key_arn or None,
        max_runtime_in_seconds=cfg.preprocessing.compute.max_runtime_seconds,
        sagemaker_session=session,
        base_job_name=f"{naming.base_job_name(cfg.model_name)}-prep",
        entrypoint=[f"{_PROGRAM_DIR}/{cfg.preprocessing.prep_entrypoint}"],
        network_config=config.vpc_network_config(),
    )

    # One input per available split, each mounted into its OWN subdir
    # (_PREP_DATA_IN/{split}/) — SageMaker rejects two inputs sharing a
    # destination. preprocess.py reads the single parquet inside each subdir.
    data_inputs = [
        ProcessingInput(
            input_name=f"data-{split}",
            s3_input=ProcessingS3Input(
                s3_uri=info["s3_uri"],
                local_path=f"{_PREP_DATA_IN}/{split}",
                s3_data_type="S3Prefix",
            ),
        )
        for split, info in resolved_channels.items()
    ]

    # The transform list rides in as its own single-object input, mounted alongside
    # the data. s3_uri names the exact object, so it lands at
    # _PREP_STEPS_IN/<basename> and the container reads that path.
    steps_uri, steps_name = _upload_prep_steps(cfg, session, kms_key_arn)
    steps_input = ProcessingInput(
        input_name="steps",
        s3_input=ProcessingS3Input(
            s3_uri=steps_uri,
            local_path=_PREP_STEPS_IN,
            s3_data_type="S3Prefix",
        ),
    )

    facets = cfg.evaluation.bias_facet_columns if cfg.evaluation else []
    prep_args = processor.run(
        inputs=[*data_inputs, steps_input],
        outputs=[
            ProcessingOutput(
                output_name="prep",
                s3_output=ProcessingS3Output(
                    s3_uri=out_uri,
                    local_path=_PREP_OUT,
                    s3_upload_mode="EndOfJob",
                ),
            ),
        ],
        # Columns go as SEPARATE tokens (not comma-joined): SageMaker caps one
        # ContainerArgument at 256 chars and a real feature list exceeds that.
        arguments=[
            # Omitted entirely when the config authors no feature list: blank means
            # "every column except the target and the drops", which only the
            # container can resolve because only it has read the dataset. Passing a
            # bare --feature-columns instead would hand argparse a flag with no
            # values.
            *(
                ["--feature-columns", *cfg.feature_columns]
                if cfg.feature_columns
                else []
            ),
            "--target-variable",
            cfg.target_variable,
            *(["--facet-columns", *facets] if facets else []),
            # Both column filters are omitted unless authored, so an image built
            # against the older prep contract keeps working until its model opts in.
            *(["--drop-columns", *cfg.drop_columns] if cfg.drop_columns else []),
            *(["--drop-zero-variance"] if cfg.drop_zero_variance else []),
            # The transform pipeline is DATA now, not a single --scaler choice: the
            # YAML lists ordered {module, function, source_columns, params} steps
            # and the container imports and applies them in order. Passed as a
            # mounted FILE, not inline JSON, because one real step list already
            # exceeds the 256-char per-argument cap (see _upload_prep_steps).
            "--steps-file",
            f"{_PREP_STEPS_IN}/{steps_name}",
        ],
    )
    return ProcessingStep(name="Preprocess", step_args=prep_args)


def _eval_test_uri(resolved_channels):
    """S3 location of the held-out data the evaluate step scores.

    The model is scored ONLY on the holdout split — the data train never saw and
    that was NOT used for early stopping. We deliberately do NOT fall back to
    validation (the in-loop early-stopping signal) or train (the fit data): both
    yield optimistically biased numbers that would then be reported as held-out
    metrics, misleading the gate and the registry. Absent a holdout, we error out
    rather than silently evaluate on the wrong split.
    """
    if "holdout" in resolved_channels:
        return resolved_channels["holdout"]["s3_uri"]
    raise ValueError("evaluation requires a 'holdout' split.")


# Separator inside one --primary-metric token. A gate is a fixed triple
# (name, threshold, direction), so it packs into a single argument instead of three
# positionally-matched lists; ":" cannot occur in a metric name (it is a JsonGet path
# into metrics.json and a model-card metric key) or in a direction keyword.
_GATE_SEP = ":"


def _evaluation_arguments(cfg):
    """The config's evaluation block as container arguments for the evaluate script.

    Mirrors _prep_step's argument style deliberately, including WHY the lists are
    separate tokens rather than one comma-joined string: SageMaker caps a single
    ContainerArguments member at 256 chars, and a metric list is authored, not
    bounded. Optional fields are OMITTED when unset rather than passed empty, so an
    image built against the older contract — one that reads its own baked config.yaml
    and ignores unrecognised flags — keeps running until its model opts in.

      --primary-metric name:threshold:direction ...   the gate, always present
      --secondary-metrics name ...                    logged, not gated
      --run-bias-check                                flag; absent means false
      --bias-label-column name                        predictions.csv's label header
    """
    evaluation = cfg.evaluation
    # _primary_metrics() already rejects an empty list and a blank name at load
    # time, so there is always at least one gate to pass on here.
    gates = [
        _GATE_SEP.join([m.metric, str(m.threshold), m.threshold_direction])
        for m in evaluation.primary_metrics
    ]
    return [
        "--primary-metric",
        *gates,
        *(
            ["--secondary-metrics", *evaluation.secondary_metrics]
            if evaluation.secondary_metrics
            else []
        ),
        *(["--run-bias-check"] if evaluation.run_bias_check else []),
        # Sent whether or not bias runs, because it names a column the container
        # writes unconditionally. It is the one part of bias_config the container
        # cannot be left to guess: _prediction_headers() below tells Clarify this
        # same name, and Clarify matches headers to columns by POSITION, so a
        # container that wrote a different header would still line up — reporting
        # bias on the right column under a name the file never used, with nothing
        # raising. (Everything else in bias_config is consumed by the Clarify step
        # itself, and WHICH facets reached the holdout is known to prep, not here.)
        "--bias-label-column",
        evaluation.bias_label_column,
    ]


def _evaluate_step(
    cfg, train_step, session, role, kms_key_arn, image_uri, test_s3_uri, out_uri
):
    """Build the record-only Evaluate ProcessingStep (AR-08-b).

    Runs on the dedicated evaluate image for this model (image_uri here =
    naming.step_image_ref(model_name, "evaluate") resolved to a full URI) and runs its
    baked-in eval launcher as the entrypoint. Inputs: the trained model.tar.gz (from the
    Train step's ModelArtifacts) at /opt/ml/processing/input/model, and the held-out
    test data at /opt/ml/processing/input/test (test_s3_uri). With prep that URI is
    prep's output — X_test.npy/y_test.npy/split_test.parquet — not raw parquet.
    Output: metrics.json + predictions.csv to the evaluation prefix. Eval gets its
    OWN hardware (compute.evaluation).

    The config's evaluation block travels as container ARGUMENTS — the same carrier
    prep uses for the data/preprocessing blocks, and the Processing-job counterpart
    of the SM_HP_* channel that carries the algorithm block's hyperparameters into
    the training job. So all three stages read one file: the training config.
    (Before this, the evaluate image baked its own copy of the evaluation block
    beside the eval script, which nothing cross-checked — the container gated on one
    threshold while the model card and the onboarding comparison read another.)

    NOT a gate: the step produces the metric-vs-threshold evidence but never halts
    the pipeline (no ConditionStep); Register just depends_on it for ordering.
    """
    processor = Processor(
        image_uri=image_uri,
        role=role,
        instance_count=cfg.evaluation.compute.instance_count,
        instance_type=cfg.evaluation.compute.instance_type,
        volume_size_in_gb=cfg.evaluation.compute.volume_size_gb,
        volume_kms_key=kms_key_arn or None,
        output_kms_key=kms_key_arn or None,
        max_runtime_in_seconds=cfg.evaluation.compute.max_runtime_seconds,
        sagemaker_session=session,
        base_job_name=f"{naming.base_job_name(cfg.model_name)}-eval",
        entrypoint=[f"{_PROGRAM_DIR}/{cfg.evaluation.eval_entrypoint}"],
        network_config=config.vpc_network_config(),
    )

    eval_args = processor.run(
        inputs=[
            ProcessingInput(
                input_name="model",
                s3_input=ProcessingS3Input(
                    s3_uri=train_step.properties.ModelArtifacts.S3ModelArtifacts,
                    local_path=_MODEL_IN,
                    s3_data_type="S3Prefix",
                ),
            ),
            ProcessingInput(
                input_name="test",
                s3_input=ProcessingS3Input(
                    s3_uri=test_s3_uri,
                    local_path=_TEST_IN,
                    s3_data_type="S3Prefix",
                ),
            ),
        ],
        outputs=[
            ProcessingOutput(
                output_name="evaluation",
                s3_output=ProcessingS3Output(
                    s3_uri=out_uri,
                    local_path=_EVAL_OUT,
                    s3_upload_mode="EndOfJob",
                ),
            ),
        ],
        arguments=_evaluation_arguments(cfg),
    )
    # Register metrics.json as a PropertyFile so RegisterModel can JsonGet the
    # held-out numbers at RUNTIME and Join them into the model card's
    # training_metrics — resolved in-pipeline (no post-register
    # UpdateModelPackage API call).
    metrics_pf = PropertyFile(
        name=_EVAL_METRICS_PF,
        output_name="evaluation",
        path="metrics.json",
    )
    return ProcessingStep(
        name="Evaluate",
        step_args=eval_args,
        property_files=[metrics_pf],
    )


# predictions.csv is a platform CONTRACT (the evaluate container writes it in this
# fixed shape): the facet column(s), then label/predicted_label/probability. Clarify
# needs the exact headers at pipeline-BUILD time (the file doesn't exist yet), so we
# reconstruct them from the config instead of reading the CSV. label/predicted_label
# are non-numeric class tokens ("yes"/"no") so smclarify classifies them CATEGORICAL,
# not CONTINUOUS — see the encoding note in the sample evaluate.py.
#
# Reconstructing is only safe because the container is TOLD the label column's name
# (_evaluation_arguments passes --bias-label-column), so both sides read one field.
# Without that, the names below and the file's own headers could differ while the
# columns still lined up by position, and the reports would name the wrong column.
#
# Every column NAME and every bias choice below comes from evaluation.bias_config —
# nothing about this model's label, its positive outcome or its protected attributes
# is decided in platform code. The one exception is the trailing score column: no
# YAML field names it, the evaluate image decides at BUILD time whether to emit it
# (--emit-predictions), and Clarify never reads it (it is excluded from the analysis).
_PROB_COL = "probability"


def _prediction_headers(cfg):
    """The predictions.csv column order the evaluate container emits (the contract)."""
    return list(cfg.evaluation.bias_facet_columns) + [
        cfg.evaluation.bias_label_column,
        cfg.evaluation.bias_predicted_label_column,
        _PROB_COL,
    ]


def _clarify_bias_step(
    cfg, eval_step, session, role, kms_key_arn, predictions_uri, out_uri
):
    """Build the record-only Clarify bias ProcessingStep (AR-08-b).

    Runs SageMaker Clarify (an AWS-MANAGED image — no BYO container here) on the
    predictions.csv the evaluate step published. POST-training bias reads the
    predicted_label column DIRECTLY (DataConfig(predicted_label=...), model_config
    omitted) — so there is NO model invocation and NO endpoint (the M7 inference
    image is not needed). depends_on=[Evaluate] so it runs after predictions.csv
    exists; like evaluate it is record-only and never gates registration.

    EVERY Clarify input is authored in evaluation.bias_config: the positive outcome,
    ALL facet columns (not just the first) with their sensitive values, the group
    column that enables the conditional metrics CDDL/CDDPL, the prediction column,
    and which metric families to compute. A model changes any of these by editing
    its YAML, with no platform change.
    """
    bias = cfg.evaluation.bias
    facets = cfg.evaluation.bias_facet_columns
    label_col = cfg.evaluation.bias_label_column
    pred_col = cfg.evaluation.bias_predicted_label_column
    headers = _prediction_headers(cfg)
    # Everything that is neither the label, the prediction, nor a facet is not a
    # model feature here (e.g. probability) — exclude it from the analysis.
    analysed = {label_col, pred_col, *facets}
    excluded = [c for c in headers if c not in analysed]

    if not bias.label_values_or_threshold:
        raise ValueError(
            "evaluation.bias_config.label_values_or_threshold is required when "
            "run_bias_check is true: Clarify has no way to guess which value(s) of "
            f"'{label_col}' are the positive outcome, and an empty list makes every "
            "bias metric meaningless rather than failing loudly."
        )

    clarify = SageMakerClarifyProcessor(
        role=role,
        instance_count=cfg.evaluation.compute.instance_count,
        instance_type=cfg.evaluation.compute.instance_type,
        volume_kms_key=kms_key_arn or None,
        output_kms_key=kms_key_arn or None,
        sagemaker_session=session,
        job_name_prefix=f"{naming.base_job_name(cfg.model_name)}-bias"[:63],
        network_config=config.vpc_network_config(),
    )

    # s3_output_path is run-scoped (out_uri) so each execution's analysis lands in
    # its own folder. But Clarify uploads analysis_config.json to
    # (s3_analysis_config_output_path or s3_output_path) at BUILD time — a runtime
    # Join can't be a build-time upload target, so we pin the config output to a
    # CONCRETE per-model path. The config is identical across runs (same headers/
    # facet), so sharing that one location is safe; only the per-run RESULTS are
    # namespaced.
    data_config = DataConfig(
        s3_data_input_path=predictions_uri,
        s3_output_path=out_uri,
        s3_analysis_config_output_path=f"{naming.bias_output_s3(cfg.model_name)}/analysis-config",
        label=label_col,
        predicted_label=pred_col,
        headers=headers,
        dataset_type=_CSV_CONTENT_TYPE,
        excluded_columns=excluded or None,
    )
    # facet_name takes the WHOLE list: Clarify computes each facet's metrics in the
    # one job, so a config naming two protected attributes gets both reported instead
    # of silently only the first. group_name is optional — without it the conditional
    # metrics (CDDL pre, CDDPL post) come back null, which is a legitimate choice.
    bias_config = BiasConfig(
        label_values_or_threshold=list(bias.label_values_or_threshold),
        facet_name=list(facets),
        facet_values_or_threshold=cfg.evaluation.bias_facet_values(),
        group_name=bias.group_name or None,
    )

    bias_args = clarify.run_bias(
        data_config=data_config,
        bias_config=bias_config,
        pre_training_methods=bias.pre_training_metrics,
        post_training_methods=bias.post_training_metrics,
        kms_key=kms_key_arn or None,
    )
    return ProcessingStep(
        name="BiasCheck",
        step_args=bias_args,
        depends_on=[eval_step],
    )


def pipeline_tags(cfg):
    """TrainingConfig tags (+ DataZone tags) -> CreatePipeline Tags (CamelCase Key/Value).

    Applied at the pipeline level (pipe.upsert(tags=...)), NOT per step. The v3
    Tag object serializes to lowercase {key,value}, which CreatePipeline rejects
    inside a step's job definition ('Unknown Argument member key'); the pipeline
    resource takes the {Key,Value} API shape and the training job inherits these.

    The DataZone domain/project tags (config.datazone_tags) ride along here so the
    pipeline AND the model package the RegisterModel step creates both surface
    under the AR project in SageMaker Unified Studio (the registered package
    inherits the pipeline's tags — hence no separate registry-tag path needed)."""
    merged = dict(cfg.all_tags())
    merged.update(config.datazone_tags())
    return [{"Key": k, "Value": str(v)} for k, v in merged.items()]


def _hyper_parameters_fragment(hyperparameters):
    """JSON fragment for the card's training_job_details.hyper_parameters.

    This is the ONE place the model-card schema exposes hyperparameters — Unified
    Studio's "Hyperparameters" section on the model version page reads it from HERE,
    not from DescribeTrainingJob. Unlike the held-out metrics (which need a runtime
    JsonGet because they don't exist until Evaluate runs), hyperparameters are static
    config known at BUILD time, so they serialize straight to literals.

    Returns the fragment '"hyper_parameters":[{"name","value"},...]' (no surrounding
    separators) so callers can splice it into training_job_details. Values are
    stringified (the card schema types hyper_parameter values as strings).
    """
    entries = ",".join(
        '{"name":' + json.dumps(str(k)) + ',"value":' + json.dumps(str(v)) + "}"
        for k, v in hyperparameters.items()
    )
    return '"hyper_parameters":[' + entries + "]"


def _splice_artifact(text, artifact_uri):
    """Split a JSON fragment on the artifact sentinel, splicing the runtime URI in.

    The card's model_overview.model_artifact is serialized at build time with the
    _ARTIFACT_SENTINEL placeholder (a pipeline property can't live in a JSON string),
    so `text` contains the literal sentinel wherever the real S3 URI belongs. We split
    on it and interleave `artifact_uri` — the Train step's runtime
    ModelArtifacts.S3ModelArtifacts property — so the Join resolves the real
    run-scoped model.tar.gz path at register time. artifact_uri is a bare S3 string
    needing no JSON escaping. Returns a list of Join pieces (literal chunks with the
    property object between them); if the sentinel isn't present, returns [text].
    """
    parts = text.split(_ARTIFACT_SENTINEL)
    pieces = [parts[0]]
    for part in parts[1:]:
        pieces.append(artifact_uri)
        pieces.append(part)
    return pieces


def _merge_narrative(narrative_json, head_close, artifact_uri):
    """Append the onboarding narrative object's fields onto a training_details head.

    head_close is the training_details JSON up to (and including) its closing braces
    but WITHOUT the outer object's closing '}'. We strip the narrative's outer braces
    and splice its fields in as siblings, then close the outer object. The narrative
    carries the artifact sentinel (model_overview.model_artifact), so we run it through
    _splice_artifact to inject the runtime URI. Shared by both card paths so the merge
    logic lives in one place. Returns the list of Join pieces to append.
    """
    inner = narrative_json[1:-1]
    if not inner:
        return [head_close, "}"]
    return [head_close, ","] + _splice_artifact(inner, artifact_uri) + ["}"]


def _training_metrics_join(narrative_json, hyperparameters, artifact_uri, metric_names):
    """Build the card content as a Join that splices RUNTIME values into the card.

    The card content is a JSON string; register() freezes it at build time, so the
    held-out numbers (which don't exist until Evaluate runs) and the run-scoped model
    artifact URI (the Train step's output) can't be literals. So we emit the card as a
    Join whose training_metrics VALUES are JsonGet expressions reading the Evaluate
    step's metrics.json PropertyFile, and whose model_artifact is the Train step's
    S3ModelArtifacts property — both resolved when the pipeline runs, so the metric
    boxes AND the artifact Location fill at register time (no post-register step).

    Target shape = exactly what Unified Studio renders:
        training_details.training_job_details.{hyper_parameters, training_metrics}
    hyper_parameters are static build-time literals; training_metrics are runtime
    JsonGets. metric_names comes from the YAML (evaluation primary + secondary metric
    names, via cfg.evaluation.card_metric_names) — each name is both the JsonGet path
    into metrics.json AND the emitted card name (the evaluate container writes these
    exact keys). narrative_json is the onboarding card content serialized to JSON
    ('{...}' or '{}'); we merge the two into one object.
    """
    values = [
        '{"training_details":{"training_job_details":{'
        + _hyper_parameters_fragment(hyperparameters)
        + ',"training_metrics":['
    ]
    for i, key in enumerate(metric_names):
        # JSON list separator between entries; name is a build-time literal, value
        # is the runtime JsonGet scalar from metrics.json.
        values.append(("," if i else "") + '{"name":' + json.dumps(key) + ',"value":')
        values.append(
            JsonGet(
                step_name="Evaluate",
                property_file=_EVAL_METRICS_PF,
                json_path=key,
            )
        )
        values.append("}")
    # Close the metrics array + training_job_details + training_details, then merge.
    values.extend(_merge_narrative(narrative_json, "]}}", artifact_uri))
    return Join(on="", values=values)


def _training_hyperparameters_content(narrative_json, hyperparameters, artifact_uri):
    """Card content (a Join) with hyperparameters but NO metrics.

    Used when there is no Evaluate step: hyperparameters are still static build-time
    config, so they're written into training_job_details regardless (Unified Studio's
    Hyperparameters section needs them). No metric JsonGets are needed, but the card
    still carries the run-scoped model artifact URI (the Train step's runtime
    property), so this is a Join too (not a plain string). Same narrative merge as the
    with-metrics path.
    """
    head = '{"training_details":{"training_job_details":{' + _hyper_parameters_fragment(
        hyperparameters
    )
    # "}}" closes training_job_details + training_details; then merge narrative.
    return Join(
        on="", values=_merge_narrative(narrative_json, head + "}}", artifact_uri)
    )


def _bullets(items):
    """Render a form's YAML list as a newline bullet list (None when empty).

    The card content is free text, so lists have to be flattened; None rather
    than "" keeps the field out of the serialized card entirely.
    """
    return "\n".join(f"- {i}" for i in items) if items else None


def _card_overview(cfg, form, artifact_uri):
    """model_overview — the form's business wording over the config's identity.

    model_artifact carries the _ARTIFACT_SENTINEL placeholder, not artifact_uri:
    the real value is a runtime pipeline property spliced in after serialization
    (see _model_card). Omitted entirely when there is no artifact to point at.
    """
    return ModelOverview(
        # The onboarding form's description leads (it is the business-reviewed
        # wording); identity.description is the DS's own and covers a run with no
        # form, so the card is never left without one.
        model_description=form.description or cfg.description or None,
        model_creator=form.business_unit or cfg.business_unit or None,
        model_artifact=[_ARTIFACT_SENTINEL] if artifact_uri is not None else None,
        algorithm_type=cfg.image_ref or None,
        problem_type=form.target_type or None,
        model_owner=cfg.owner or None,
    )


def _card_intended_uses(form):
    """intended_uses — purpose, approved uses, limitations, fairness."""
    return IntendedUses(
        purpose_of_model=form.purpose or None,
        intended_uses=_bullets(form.approved_uses),
        factors_affecting_model_efficiency=form.limitations or None,
        explanations_for_risk_rating=form.fairness_considerations or None,
    )


def _card_business_details(form):
    """business_details — the business problem and who owns it."""
    return BusinessDetails(
        business_problem=form.use_case or None,
        business_stakeholders=form.business_sponsor or None,
        line_of_business=form.owner_bu or form.business_unit or None,
    )


def _card_caveats(form):
    """Limitations, with the prohibited uses appended under their own heading.

    The card has no field for prohibited uses, so they ride along with the
    caveats — the nearest governance-reviewed slot. None when the form carries
    neither.
    """
    if not form.prohibited_uses:
        return form.limitations or None
    return form.limitations + "\n\nProhibited uses:\n" + _bullets(form.prohibited_uses)


def _card_additional_information(cfg, form):
    """additional_information — fairness, caveats, and machine-readable details.

    custom_details keeps the key/values (model name, update cadence) that don't
    fit the card's free-text shapes; blank ones are dropped rather than sent as "".
    """
    custom = {
        "model_name": cfg.model_name,
        "model_version": str(cfg.model_version),
        "update_frequency": form.update_frequency,
        "data_classification": form.data_classification,
    }
    return AdditionalInformation(
        ethical_considerations=form.fairness_considerations or None,
        caveats_and_recommendations=_card_caveats(form),
        custom_details={k: v for k, v in custom.items() if v},
    )


def _model_card(cfg, form, artifact_uri, *, with_metrics=False):
    """Build the ModelPackageModelCard from the onboarding form (AR-08-c).

    The onboarding form (FORM #1, one per model) is the SOURCE of the governance
    narrative — purpose, limitations, approved/prohibited uses, fairness. The
    training config (per run) supplies the runtime identity — owner/team,
    problem type, and the trained artifact URI. Every card field is optional, so
    a sparse form still yields a valid card; form=None yields a metrics-only card.

    artifact_uri is the Train step's runtime ModelArtifacts.S3ModelArtifacts property
    (the run-scoped model.tar.gz) — a pipeline expression, NOT a build-time string. It
    can't be serialized into the card's JSON content, so model_overview.model_artifact
    is serialized with the _ARTIFACT_SENTINEL placeholder and the real property is
    spliced back in via the Join (see _splice_artifact). This makes the card's
    "Location" (classic Studio) match the InferenceSpec ModelDataUrl (Unified Studio).

    Uses are rendered as newline bullet lists (the card content is free text;
    the form carries them as YAML lists). custom_details keeps machine-readable
    key/values (model name, update cadence) that don't fit the free-text shapes.

    Hyperparameters (static build-time config) are ALWAYS written into the card's
    training_job_details — Unified Studio's Hyperparameters section reads them there.
    with_metrics=True (an Evaluate step exists) additionally splices the held-out
    training_metrics in as runtime JsonGets, so the card carries the real numbers at
    register time (no post-register step); without it, the card carries just the
    hyperparameters + narrative. Either way the content is a Join (both paths carry the
    runtime artifact_uri).
    """
    form = form or OnboardingForm(model_name=cfg.model_name)
    content = ModelCardContent(
        model_overview=_card_overview(cfg, form, artifact_uri),
        intended_uses=_card_intended_uses(form),
        business_details=_card_business_details(form),
        additional_information=_card_additional_information(cfg, form),
    )
    # register() takes the content as a JSON string; status Draft — the package's
    # own approval_status (PendingManualApproval) governs promotion, not the card.
    narrative_json = content.model_dump_json(exclude_none=True)
    # target_variable travels to the container as a hyperparameter but is not a model
    # hyperparameter — keep the card's list to the real algorithm.hyperparameters.
    hyperparameters = dict(cfg.hyperparameters)
    # The card's training_metrics are the metric names the YAML lists (primary gate
    # metrics + secondary), NOT a hardcoded set. with_metrics is set only when an
    # Evaluate step exists, so cfg.evaluation is present here.
    card_content = (
        _training_metrics_join(
            narrative_json,
            hyperparameters,
            artifact_uri,
            cfg.evaluation.card_metric_names(),
        )
        if with_metrics
        else _training_hyperparameters_content(
            narrative_json, hyperparameters, artifact_uri
        )
    )
    return ModelPackageModelCard(
        model_card_content=card_content,
        model_card_status="Draft",
    )


def _model_metrics(cfg, *, with_bias, metrics_uri, bias_analysis_uri):
    """Point the registered package's ModelMetrics at the evaluate/bias evidence.

    The held-out numbers (accuracy/precision/recall/f1/auc) and the Clarify analysis are RUNTIME
    outputs — they don't exist at build time, so (like the model card's report_s3_uri
    and Clarify reading predictions.csv) we reference their S3 URIs, not the values.
    Those URIs are RUN-SCOPED (execution-ID namespaced) pipeline expressions, so the
    package records this run's own evidence folder. MetricsSource takes an S3 URI, so
    the package records WHERE the metrics are; Register depends_on Evaluate (+
    BiasCheck), so the files exist by the time the package is created. This is what
    renders as 'Model quality' (+ bias) in the registry console, distinct from the
    card's free-text narrative.

    Returns None when there is no evaluation block (nothing to reference).
    """
    if cfg.evaluation is None:
        return None
    metrics = ModelMetrics(
        model_statistics=MetricsSource(
            content_type=_JSON_CONTENT_TYPE,
            s3_uri=metrics_uri,
        ),
    )
    if with_bias:
        metrics.bias_post_training = MetricsSource(
            content_type=_JSON_CONTENT_TYPE,
            s3_uri=bias_analysis_uri,
        )
    return metrics


# SageMaker caps ModelPackageDescription at 1024 characters. identity.description is
# free text, so a long one gets trimmed rather than failing the register step.
_DESCRIPTION_LIMIT = 1024


def _serving_image_uri(cfg, serving_instance):
    """The registered version's SERVING container — model-agnostic by DEFAULT.

    The image recorded on the version is NOT the container that trained the
    model: train writes model.tar.gz, serve reloads it behind /ping +
    /invocations. The serving image is model-agnostic by default — the model's
    serving code rides in model.tar.gz's code/ — so it's one of the two SHARED
    platform images (config.inference_image_ref), picked by CPU/GPU family, not
    a per-model derived repo. No YAML change is needed to get a real serving
    container on the registered version in the common case.

    A model whose inference.py needs a dependency the shared image does not
    carry (e.g. xgboost — see src/images/job-inference-cpu/requirements.txt)
    authors compute.inference.local_requirements or .dockerfile, same as it
    would for train/evaluate. Either one is the signal that switches this to
    the per-model repo naming.step_image_ref already computes for every other
    stage — the shared path is otherwise unchanged, so every model that
    authors neither keeps resolving the same two platform images as before
    this branch existed.

    `serving_instance` is taken as a parameter, not recomputed here, because
    build_pipeline also records it as the version's recommended inference/
    transform hardware — one call site stays the single source of truth.
    """
    owns_serving_image = cfg.inference and (
        cfg.inference.local_requirements or cfg.inference.dockerfile
    )
    if owns_serving_image:
        return config.resolve_image_uri(
            naming.step_image_ref(cfg.model_name, "inference", serving_instance)
        )
    return config.resolve_image_uri(config.inference_image_ref(serving_instance))


def _package_description(cfg):
    """The registered version's one-line description.

    identity.description leads: it is the DS's own words for what the model does,
    which is why the template requires it and why it is carried on TrainingConfig.
    Falls back to the model name for a config that somehow has none.

    The version is appended ONLY when metadata.version authors one. Unauthored it
    is the "auto" sentinel, and SageMaker assigns the real version at register
    time — printing the sentinel put the literal string "vauto" on every package.
    """
    head = cfg.description.strip() or cfg.model_name
    version = f" v{cfg.model_version}" if cfg.model_version != "auto" else ""
    text = f"{head}{version} (owner={cfg.owner}, bu={cfg.business_unit})"
    if len(text) <= _DESCRIPTION_LIMIT:
        return text
    return text[: _DESCRIPTION_LIMIT - 3] + "..."


def _prep_and_train_input(
    cfg, resolved_channels, session, role, kms_key_arn, prep_out_uri
):
    """(prep_step, train_input_config) — prep is optional, train input always is not.

    When the config carries a preprocessing block, prep runs FIRST on its OWN
    dedicated image (naming.step_image_ref(.., "preprocess")) and produces the
    scaled .npy arrays the BYO container expects; Train then takes a SINGLE
    'train' channel = prep's output (which holds all the arrays + scaler.pkl
    the launcher stages into the model artifact). Without a preprocessing
    block, the raw M4 parquet channels feed the container directly, unchanged
    (train-only flow / a container that reads parquet itself) — prep_step is
    then None, which build_pipeline uses to skip Steps 2/3's prep-vs-raw branch
    too.
    """
    if cfg.preprocessing is None:
        return None, _channels(resolved_channels)

    prep_image_uri = config.resolve_image_uri(cfg.preprocessing.image_ref)
    prep_step = _prep_step(
        cfg,
        resolved_channels,
        session,
        role,
        kms_key_arn,
        prep_image_uri,
        prep_out_uri,
    )
    train_input_config = [
        Channel(
            channel_name="train",
            data_source=DataSource(
                s3_data_source=S3DataSource(
                    s3_data_type="S3Prefix",
                    s3_uri=prep_step.properties.ProcessingOutputConfig.Outputs[
                        "prep"
                    ].S3Output.S3Uri,
                    s3_data_distribution_type="FullyReplicated",
                )
            ),
        )
    ]
    return prep_step, train_input_config


def _evaluate_and_bias_steps(
    cfg,
    train_step,
    prep_step,
    resolved_channels,
    session,
    role,
    kms_key_arn,
    eval_out_uri,
    predictions_uri,
    bias_out_uri,
):
    """(eval_step, bias_step) — both optional, both record-only (AR-08-b): they
    score/assess the model but never gate registration.

    Evaluate runs only when cfg.evaluation is set, on its OWN dedicated image
    (naming.step_image_ref(.., "evaluate")); with prep, it reads prep's output
    (X_test.npy / y_test.npy / split_test.parquet) rather than the raw holdout
    parquet. The Clarify bias step runs only nested under Evaluate, and only
    when the config both asks for it AND declares a facet column (the AC
    drives Clarify off evaluation.bias_config.facet_columns, not the
    onboarding form's free-text field).
    """
    if cfg.evaluation is None:
        return None, None

    test_s3 = (
        prep_step.properties.ProcessingOutputConfig.Outputs["prep"].S3Output.S3Uri
        if prep_step is not None
        else _eval_test_uri(resolved_channels)
    )
    eval_image_uri = config.resolve_image_uri(cfg.evaluation.image_ref)
    eval_step = _evaluate_step(
        cfg,
        train_step,
        session,
        role,
        kms_key_arn,
        eval_image_uri,
        test_s3,
        eval_out_uri,
    )

    bias_step = None
    if cfg.evaluation.run_bias_check and cfg.evaluation.bias_facet_columns:
        bias_step = _clarify_bias_step(
            cfg,
            eval_step,
            session,
            role,
            kms_key_arn,
            predictions_uri,
            bias_out_uri,
        )
    return eval_step, bias_step


def _register_customer_metadata(cfg, eval_step, bias_step, eval_out_uri, bias_out_uri):
    """CustomerMetadataProperties pinning the evaluation evidence's S3 location
    onto the package, so a reviewer/M6 can find the report from the registry
    entry (AR-08-c AC-3).

    Per the DS team we share the bias output FOLDER when Clarify ran (it holds
    the combined bias + report evidence); otherwise the raw evaluate output
    folder; otherwise just the identity, when neither ran.
    """
    customer_metadata = {
        "model_name": cfg.model_name,
        "model_version": str(cfg.model_version),
    }
    if bias_step is not None:
        customer_metadata["report_s3_uri"] = bias_out_uri
    elif eval_step is not None:
        customer_metadata["report_s3_uri"] = eval_out_uri
    return customer_metadata


def build_pipeline(
    cfg,
    resolved_channels,
    *,
    pipeline_session=None,
    role=None,
    kms_key_arn=None,
    onboarding=None,
):
    """Build (not run) the train (+ evaluate) + register Pipeline.

    Args:
        cfg: a validated model_training_core.training_config.TrainingConfig.
        resolved_channels: output of model_training_core.datasets.resolve_channels(cfg.data)
            — {split: {s3_uri, version, split}}. 'train' is required.
        pipeline_session: a PipelineSession (defaults to one on the configured region).
        role: execution role ARN (defaults to config.execution_role_arn()).
        kms_key_arn: CMK for artifact encryption (defaults to config.KMS_KEY_ARN;
            empty -> SSE-S3 default). Consumed at provisioning (needs CreateGrant).
        onboarding: an optional model_training_core.onboarding_form.OnboardingForm. When present,
            the registered model package gets a Model Card (AR-08-c) with its
            narrative seeded from the form; when None, the card carries metrics only.
            The card is built whenever a form OR an Evaluate step is present — so a
            train-only run with neither registers with no card at all. When an
            Evaluate step is present, the held-out metrics are spliced into the card
            at RegisterModel via a runtime Join (see _training_metrics_join), so the
            version's Performance/Custom boxes are filled with no post-register step.
            Never gates registration.

    Returns the sagemaker.mlops.workflow.pipeline.Pipeline (call .upsert() then
    .start() to run). Prep is present only when cfg.preprocessing is set; Evaluate
    only when cfg.evaluation is set; the Clarify bias step only when that evaluation
    block enables run_bias_check with a facet column.
    """
    # Pin the session's default bucket to the AR artifact bucket. Without this,
    # SDK code paths that fall back to Session.default_bucket() (e.g. ModelBuilder
    # code upload in register()) target the SageMaker convention bucket
    # sagemaker-<region>-<account>, which this account/role cannot access (403 on
    # HeadBucket). ARTIFACT_PREFIX namespaces those uploads alongside the run output.
    session = pipeline_session or PipelineSession(
        boto_session=config.session(),
        default_bucket=config.ARTIFACT_BUCKET,
        default_bucket_prefix=config.ARTIFACT_PREFIX,
    )
    role = role or config.execution_role_arn()
    kms_key_arn = kms_key_arn if kms_key_arn is not None else config.KMS_KEY_ARN
    image_uri = config.resolve_image_uri(cfg.image_ref)

    # A pipeline parameter for approval status so a run can override it later
    # (e.g. Approved in an automated QA flow) without rebuilding the definition.
    approval_status = ParameterString(
        name="ModelApprovalStatus", default_value="PendingManualApproval"
    )

    # Target column travels to the container as a hyperparameter so BYO code
    # knows the label without hardcoding it.
    hyperparameters = dict(cfg.hyperparameters)
    hyperparameters.setdefault("target_variable", cfg.target_variable)

    # Run-scope ALL run outputs by execution ID so repeated or concurrent runs
    # don't overwrite each other. The execution id sits right after the per-model
    # run root, BEFORE the step leaf dir, so one run's prep/evaluate/bias/output all
    # live under a single …/<exec-id>/ folder. Computed ONCE here and threaded into
    # the step builders so every producer and its consumers (Clarify reading
    # predictions, ModelMetrics pointing at metrics/bias) share the identical
    # expression -> the same S3 folder at runtime. Prep's output is consumed by
    # Train/Evaluate via the step's runtime PROPERTY, so those follow automatically.
    run_root = naming.model_run_root(cfg.model_name)
    prep_out_uri = _run_scoped(run_root, naming.PREP_DIR)
    eval_out_uri = _run_scoped(run_root, naming.EVAL_DIR)
    metrics_uri = _run_scoped(run_root, naming.EVAL_DIR, "metrics.json")
    predictions_uri = _run_scoped(run_root, naming.EVAL_DIR, "predictions.csv")
    bias_out_uri = _run_scoped(run_root, naming.BIAS_DIR)
    bias_analysis_uri = _run_scoped(run_root, naming.BIAS_DIR, "analysis.json")
    # Training artifacts (model.tar.gz) land under the SAME run folder; SageMaker
    # still nests its own <training-job-name>/ beneath this, so it's namespaced
    # twice over — the run folder for grouping, the job name for uniqueness.
    train_out_uri = _run_scoped(run_root, naming.OUTPUT_DIR)

    # ---- Step 0 (optional): preprocessing ----
    # When the config carries a preprocessing block, prep runs FIRST and produces
    # the scaled .npy arrays the BYO container expects. Train + Evaluate then read
    # prep's output instead of the raw M4 parquet. Without it, the raw parquet
    # splits feed the container directly (train-only flow / a container that reads
    # parquet itself). See _prep_and_train_input for the full branch.
    prep_step, train_input_config = _prep_and_train_input(
        cfg, resolved_channels, session, role, kms_key_arn, prep_out_uri
    )

    # ---- Step 1: training (BYO container, Path A) ----
    trainer = ModelTrainer(
        training_image=image_uri,
        role=role,
        sagemaker_session=session,
        base_job_name=naming.base_job_name(cfg.model_name),
        compute=Compute(
            instance_type=cfg.compute.instance_type,
            instance_count=cfg.compute.instance_count,
            volume_size_in_gb=cfg.compute.volume_size_gb,
            volume_kms_key_id=kms_key_arn or None,
        ),
        stopping_condition=StoppingCondition(
            max_runtime_in_seconds=cfg.compute.max_runtime_seconds,
        ),
        output_data_config=OutputDataConfig(
            s3_output_path=train_out_uri,
            kms_key_id=kms_key_arn or None,
        ),
        hyperparameters=hyperparameters,
        networking=config.vpc_networking(),
    )

    train_args = trainer.train(input_data_config=train_input_config)
    train_step = TrainingStep(name="Train", step_args=train_args)

    # ---- Steps 2+3 (both optional): evaluate on the holdout, then Clarify bias ----
    # Record-only (AR-08-b): scores/assesses the model, never gates registration.
    # See _evaluate_and_bias_steps for the full branch.
    eval_step, bias_step = _evaluate_and_bias_steps(
        cfg,
        train_step,
        prep_step,
        resolved_channels,
        session,
        role,
        kms_key_arn,
        eval_out_uri,
        predictions_uri,
        bias_out_uri,
    )

    # ---- Step 4: register the trained artifact ----
    serving_instance = (cfg.inference or cfg.compute).instance_type
    serving_image_uri = _serving_image_uri(cfg, serving_instance)
    builder = ModelBuilder(
        image_uri=serving_image_uri,
        s3_model_data_url=train_step.properties.ModelArtifacts.S3ModelArtifacts,
        role_arn=role,
        sagemaker_session=session,
        network=config.vpc_networking(),
    )

    # Model Card (AR-08-c): the narrative is seeded from the onboarding form when
    # supplied; the held-out metrics are spliced in as a runtime Join when an
    # Evaluate step exists (with_metrics), so the metric boxes fill at register time.
    # Built whenever EITHER a form or an eval step is present
    # (a form-less run still gets a metrics-only card). The card's model_artifact is
    # the SAME run-scoped Train output (S3ModelArtifacts) the InferenceSpec records,
    # spliced into the JSON content via Join, so classic Studio's "Location" matches
    # Unified Studio's "Artifacts"; the metrics resolve from the Evaluate PropertyFile.
    model_card = None
    if onboarding is not None or eval_step is not None:
        model_card = _model_card(
            cfg,
            onboarding,
            train_step.properties.ModelArtifacts.S3ModelArtifacts,
            with_metrics=eval_step is not None,
        )

    # CustomerMetadataProperties: deterministic paths, resolvable at build time
    # even though the files are written during the run (same pattern as Clarify
    # reading the predictions.csv URI before it exists). See
    # _register_customer_metadata for the full branch.
    customer_metadata = _register_customer_metadata(
        cfg, eval_step, bias_step, eval_out_uri, bias_out_uri
    )

    # ModelMetrics: the STRUCTURED quality/bias evidence (renders as 'Model quality'
    # in the registry), distinct from the card's free-text narrative. Points at the
    # RUN-SCOPED evaluate/bias output S3 URIs; bias_post_training only when Clarify ran.
    model_metrics = _model_metrics(
        cfg,
        with_bias=bias_step is not None,
        metrics_uri=metrics_uri,
        bias_analysis_uri=bias_analysis_uri,
    )

    # serving_instance (computed above, alongside the serving image) is also the
    # recommended SERVING hardware recorded on the version — the training instance
    # when the config authors no compute.inference block.
    register_args = builder.register(
        model_package_group_name=naming.model_package_group(cfg.model_name),
        approval_status=approval_status,
        content_types=[_CSV_CONTENT_TYPE],
        response_types=[_CSV_CONTENT_TYPE],
        inference_instances=[serving_instance],
        transform_instances=[serving_instance],
        description=_package_description(cfg),
        model_card=model_card,
        model_metrics=model_metrics,
        customer_metadata_properties=customer_metadata,
    )
    # Evaluate (+ bias) run between Train and Register when present. They're
    # record-only, so Register depends_on the LAST record step for ORDERING
    # (report-before-register), not for a gate verdict — Register runs regardless
    # of the metric or bias outcome.
    record_steps = [s for s in (eval_step, bias_step) if s is not None]
    register_step = ModelStep(
        name="RegisterModel",
        step_args=register_args,
        depends_on=record_steps or None,
    )

    # The Unified Studio Performance/Custom boxes read the card's
    # training_details.training_job_details.training_metrics. Those are populated
    # IN-PIPELINE at RegisterModel: _training_metrics_join JsonGets the held-out
    # numbers out of the Evaluate PropertyFile and Joins them into the card content
    # (see _model_card / with_metrics). No post-register step is required.

    # Prep leads the chain when present. Train + Evaluate reference prep's output
    # property, so SageMaker infers the data dependency automatically — prep only
    # needs to be in the steps list to be scheduled.
    steps = [s for s in (prep_step,) if s is not None]
    steps += [train_step, *record_steps, register_step]

    return Pipeline(
        name=naming.pipeline_name(cfg.model_name),
        parameters=[approval_status],
        steps=steps,
        sagemaker_session=session,
    )


# ---------------------------------------------------------------------------
# Orchestration — the two operations the target Step Function's states invoke,
# and which the dev/test CLI (scripts/run_training_pipeline.py) also calls. Kept
# HERE (beside build_pipeline) so there is exactly one importable home for the
# run logic: the SF Lambdas and the CLI import these; nothing imports scripts/.
#
#   validate()  -> SF State 1 (Validate): pure-ish structural + business checks,
#                  fail-fast in ms. No pipeline built, nothing upserted.
#   launch()    -> SF State 2 (Build & Launch): build the definition, ensure the
#                  registry group is DataZone-tagged, stage the prep script,
#                  upsert, and (optionally) start one execution.
#
# SF State 3 (Wait) is the state machine's native .sync SageMaker Pipeline
# integration — no code here.
# ---------------------------------------------------------------------------


def ensure_group_datazone_tags(cfg):
    """Make the Model Package GROUP carry the DataZone (Unified Studio) tags.

    Unified Studio's Model Registry lists model package GROUPS by their
    AmazonDataZone* tags — NOT the individual versions. The group is created
    lazily by the first register() and re-registering into an existing group does
    NOT re-tag it, so tagging only the pipeline/version leaves the group (and
    every version under it) invisible in Unified Studio. We create the group up
    front with the tags (idempotent: if it already exists, we add the tags to it),
    so register() lands versions into an already-visible group. No-op when the
    DataZone env vars are unset (config.datazone_tags() -> {})."""
    dz = config.datazone_tags()
    if not dz:
        return
    sm = config.session().client("sagemaker")
    group = naming.model_package_group(cfg.model_name)
    tags = [{"Key": k, "Value": v} for k, v in dz.items()]
    try:
        sm.create_model_package_group(ModelPackageGroupName=group, Tags=tags)
        print(f"[dz  ] created group '{group}' with DataZone tags")
    except sm.exceptions.ClientError as e:
        # Already exists — just (re)apply the tags to the existing group ARN.
        if "already" not in str(e).lower() and "exist" not in str(e).lower():
            raise
        arn = sm.describe_model_package_group(ModelPackageGroupName=group)[
            "ModelPackageGroupArn"
        ]
        sm.add_tags(ResourceArn=arn, Tags=tags)
        print(f"[dz  ] tagged existing group '{group}' with DataZone tags")


def validate(config_path, onboarding_form_path=None):
    """SF State 1 (Validate). Parse + structurally + business-validate the config.

    Loads and JSON-Schema-validates the training YAML, loads the optional
    onboarding form (checking its model_name matches and that the training gate is
    no looser than its approved success criteria), then runs the business/AWS
    validation (the M4 dataset resolves to a concrete version in S3). Raises on any
    failure so the SF state fails fast with a clear message and NO compute. The two
    pure layers run first, so a config that fails them costs no AWS call at all.

    Returns a dict: {cfg, onboarding, resolved} — the inputs launch() needs, so a
    caller that validates then launches does not re-parse.
    """
    from src.model_training_core import onboarding_form, training_config, validation

    cfg = training_config.load(config_path)
    print(f"[cfg ] model={cfg.model_name} v{cfg.model_version} image={cfg.image_ref}")
    print(
        f"[cfg ] target={cfg.target_variable} dataset={cfg.dataset_name} (v={cfg.dataset_version})"
    )

    onboarding = None
    if onboarding_form_path:
        onboarding = onboarding_form.load(onboarding_form_path)
        if onboarding.model_name != cfg.model_name:
            raise ValueError(
                f"onboarding form model_name '{onboarding.model_name}' does not match "
                f"training config model_name '{cfg.model_name}'."
            )
        validation.validate_kpi_gate(cfg, onboarding, config_path=config_path)
        print("[card] onboarding form loaded -> Model Card will be populated")
        if onboarding.success_metric and onboarding.minimum_threshold is not None:
            print(
                f"[gate] approved criteria: {onboarding.success_metric} "
                f"{onboarding.minimum_threshold} — training gate is no looser"
            )

    resolved = validation.validate_business(cfg, config_path=config_path)
    for ch, info in resolved.items():
        print(f"[data] {ch:10s} -> {info['s3_uri']} (v={info['version']})")

    return {"cfg": cfg, "onboarding": onboarding, "resolved": resolved}


def launch(cfg, resolved, onboarding=None, *, start=True, role=None):
    """SF State 2 (Build & Launch). Build the pipeline, tag the group, upsert, start.

    Assumes validate() already ran (cfg/resolved are trusted). Builds the
    definition, ensures the registry group is DataZone-tagged (Unified Studio
    visibility), upserts the definition, and — unless start=False — starts one
    execution.

    Returns a dict with the pipeline name and (when started) the execution ARN.
    """
    pipe = build_pipeline(cfg, resolved, onboarding=onboarding)
    print(f"[pipe] built '{pipe.name}' steps={[s.name for s in pipe.steps]}")

    # Group must be DataZone-tagged so it (and its versions) show in Unified Studio.
    ensure_group_datazone_tags(cfg)

    role = role or config.execution_role_arn()
    # Tags are applied at the pipeline level (the training job inherits them);
    # per-step tags break CreatePipeline's definition parse.
    pipe.upsert(role_arn=role, tags=pipeline_tags(cfg))
    print(f"[pipe] upserted (role={role})")

    if not start:
        print("[done] start=False: definition upserted, no execution started.")
        return {"pipeline_name": pipe.name, "execution_arn": None}

    execution = pipe.start()
    print(f"[run ] started execution: {execution.arn}")
    return {"pipeline_name": pipe.name, "execution_arn": execution.arn}
