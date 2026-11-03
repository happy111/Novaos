"""Environment configuration, one class per pipeline.

Every account id, region, ARN, bucket and image tag arrives as an environment
variable, fed from `cdk_pipeline/environments/<env>.json`. Nothing here is
hardcoded and nothing here is read from an intake form — the form carries
per-feature-group / per-model values, this module carries per-environment ones.

`_BaseConfig` holds what every pipeline needs and the one rule they must agree on:
an image URI is COMPOSED from the account and region, never pasted (SPEC §6.1).
Each pipeline subclasses it to declare only its own buckets, roles and image tag,
and to list the variables it requires — so a pipeline's Lambda is never made to set
another pipeline's variables, and a variable shared by all of them is added once.

`load()` (feature ingestion) and `load_cohort_label()` (M4) each read the
environment once and fail with a single message naming every variable that is
missing, rather than one KeyError at a time.

The Glue database is the one value not configured here: it is derived from the
feature group name, so it lives in the compiler.

No Snowflake variable appears in any of these. The Processing job resolves its own
connection and credential from `snowflake.env_vars`, so nothing about Snowflake
reaches a compiled intake — see `cohort_label_compiler`.
"""

import os

from src.common.image_tags import job_image_tag

# The <wl> segment of ar-<wl>-<env>-<kind>-<name>. One workload for the MVP, so the
# variable is optional and this is the placeholder until per-workload naming lands.
DEFAULT_WORKLOAD = "mvp"

_TAG_VARS = {
    "ClarityID": "TAG_CLARITY_ID",
    "DataProductName": "TAG_DATA_PRODUCT_NAME",
    "DataProductId": "TAG_DATA_PRODUCT_ID",
    "Environment": "TAG_ENVIRONMENT",
    "Component": "TAG_COMPONENT",
    "BU": "TAG_BU",
}

# What _BaseConfig itself reads, and therefore what every pipeline requires.
_SHARED_REQUIRED = [
    "ENV",
    "APP_PREFIX",
    "ACCOUNT_ID",
    "AWS_REGION",
    "CONFIG_BUCKET",
    "KMS_KEY_ARN",
    "ECR_REPO_NAME",
]


class ConfigError(Exception):
    """A required environment variable is missing or empty."""


class _BaseConfig:
    """The per-environment values every pipeline needs.

    Subclasses add their own buckets, roles and image tag, and extend `REQUIRED`
    with the variables those come from.
    """

    #: Environment variables `load` refuses to build this config without.
    REQUIRED = list(_SHARED_REQUIRED)

    def __init__(self, env):
        self.env = env["ENV"]
        self.app_prefix = env["APP_PREFIX"]
        self.workload = env.get("WORKLOAD") or DEFAULT_WORKLOAD
        self.account_id = env["ACCOUNT_ID"]
        self.region = env["AWS_REGION"]
        # Where a team stages its intake directory, read by every compiler.
        self.config_bucket = env["CONFIG_BUCKET"]
        self.kms_key_arn = env["KMS_KEY_ARN"]
        self.ecr_repo_name = env["ECR_REPO_NAME"]
        self.mandatory_tags = {
            key: env[var] for key, var in _TAG_VARS.items() if env.get(var)
        }
        # SageMaker Unified Studio (DataZone) placement — account-specific, so an
        # env var (never hardcoded infra), per model_training_core/config.py's
        # DATAZONE_DOMAIN_ID/DATAZONE_PROJECT_ID. Optional: unset in qa/prod until
        # Unified Studio is wired up there, and a lone id does nothing anyway.
        self.datazone_domain_id = env.get("AR_DATAZONE_DOMAIN_ID", "")
        self.datazone_project_id = env.get("AR_DATAZONE_PROJECT_ID", "")

    def ecr_image_uri(self, repo_name, tag):
        """Compose a full ECR URI. Account and region are infra, never in the form.

        One implementation on purpose: a second copy of this rule is a second place
        for it to drift from the platform convention.
        """
        registry = f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com"
        return f"{registry}/{repo_name}:{tag}"


class FeatureIngestionConfig(_BaseConfig):
    """Resolved per-environment configuration for the feature-ingestion pipeline."""

    REQUIRED = _SHARED_REQUIRED + [
        "COMPILED_INTAKE_BUCKET",
        "OFFLINE_STORE_S3_URI",
        "FEATURE_STORE_INGEST_ROLE_ARN",
        "FEATURE_INGESTION_IMAGE_TAG",
    ]

    def __init__(self, env):
        super().__init__(env)
        self.compiled_intake_bucket = env["COMPILED_INTAKE_BUCKET"]
        self.offline_store_s3_uri = env["OFFLINE_STORE_S3_URI"].rstrip("/")
        self.ingest_role_arn = env["FEATURE_STORE_INGEST_ROLE_ARN"]
        self.feature_ingestion_image_tag = env["FEATURE_INGESTION_IMAGE_TAG"]

    def default_processing_image_uri(self):
        """The distribution-only feature-ingestion image, built with no form.

        Not what a run pulls: the compiler always names a per-group image, because
        an image carrying one group's local transformations cannot serve another.
        This is the tag `build_and_push.sh` pushes when given no intake directory.
        """
        return self.ecr_image_uri(self.ecr_repo_name, self.feature_ingestion_image_tag)

    def group_processing_image_uri(self, source_name):
        """The feature-ingestion image built for ONE feature group.

        `source_name` is the form's own `identity.feature_group_name`, not the
        registered `ar-<wl>-<env>-fgr-<name>`: the raw field carries no environment,
        so one built image keeps the same tag in dev, qa and prod.
        """
        return self.ecr_image_uri(
            self.ecr_repo_name,
            job_image_tag(self.feature_ingestion_image_tag, source_name),
        )


class CohortLabelConfig(_BaseConfig):
    """Resolved per-environment configuration for the M4 cohort-label pipeline.

    Deliberately does not require the feature-ingestion variables: the offline
    store, the feature group ingest role and its image tag describe a pipeline this
    one does not run.
    """

    REQUIRED = _SHARED_REQUIRED + [
        "COHORT_LABEL_BUCKET",
        "COHORT_LABEL_EXECUTION_ROLE_ARN",
        "COHORT_LABEL_IMAGE_TAG",
    ]

    def __init__(self, env):
        super().__init__(env)
        # Where the compiled intake and the labelled cohort land, [Run ID] first.
        self.cohort_label_bucket = env["COHORT_LABEL_BUCKET"]
        # CohortLabelExecution — the Processing job's role, never the dev execution role.
        self.cohort_label_execution_role_arn = env["COHORT_LABEL_EXECUTION_ROLE_ARN"]
        self.cohort_label_image_tag = env["COHORT_LABEL_IMAGE_TAG"]

    def default_processing_image_uri(self):
        """The distribution-only cohort-label image, built with no form.

        Not what a run pulls: the compiler always names a per-model image, because
        an image carrying one model's local transformations cannot serve another.
        This is the tag `build_and_push.sh` pushes when given no intake directory.
        """
        return self.ecr_image_uri(self.ecr_repo_name, self.cohort_label_image_tag)

    def model_processing_image_uri(self, model_name):
        """The cohort-label image built for ONE model.

        `model_name` is the form's own `identity.model_name`, the same field
        `build_and_push.sh` reads from the same form — see `src.common.image_tags`.
        """
        return self.ecr_image_uri(
            self.ecr_repo_name,
            job_image_tag(self.cohort_label_image_tag, model_name),
        )


class ModelReadyDatasetConfig(_BaseConfig):
    """Resolved per-environment configuration for the M4 model-ready-dataset pipeline.

    Everything this pipeline writes — the compiled intake, the point-in-time join and
    split scratch, the validation reports and the versioned dataset — lands in the
    MODEL-READY-DATASET bucket.

    The three Processing jobs run as `CohortLabelExecution`, the same role the
    cohort-label job uses — it is the M4 execution role, and AC-8 grants it
    `FeatureStoreRead` and `DatasetWrite` by same-account policy attachment.
    """

    REQUIRED = _SHARED_REQUIRED + [
        "MODEL_READY_DATASET_BUCKET",
        "COHORT_LABEL_EXECUTION_ROLE_ARN",
        "MODEL_READY_DATASET_IMAGE_TAG",
        "DS_ALERT_TOPIC_ARN",
    ]

    def __init__(self, env):
        super().__init__(env)
        # The dataset area: KMS-encrypted, versioned, non-KMS puts denied (AC-9).
        self.model_ready_dataset_bucket = env["MODEL_READY_DATASET_BUCKET"]
        # Explicitly not the broad dev execution role.
        self.cohort_label_execution_role_arn = env["COHORT_LABEL_EXECUTION_ROLE_ARN"]
        self.model_ready_dataset_image_tag = env["MODEL_READY_DATASET_IMAGE_TAG"]
        # Where a failing gate names the check and the observed value (AC-8).
        self.ds_alert_topic_arn = env["DS_ALERT_TOPIC_ARN"]

    def default_processing_image_uri(self):
        """The distribution-only model-ready-dataset image, built with no form.

        Not what a run pulls: the compiler always names a per-model image, because
        an image carrying one model's local transformations cannot serve another.
        This is the tag `build_and_push.sh` pushes when given no intake directory.
        """
        return self.ecr_image_uri(
            self.ecr_repo_name, self.model_ready_dataset_image_tag
        )

    def model_processing_image_uri(self, model_name):
        """The model-ready-dataset image built for ONE model, all three stages.

        `model_name` is the form's own `identity.model_name`, the same field
        `build_and_push.sh` reads from the same form — see `src.common.image_tags`.
        """
        return self.ecr_image_uri(
            self.ecr_repo_name,
            job_image_tag(self.model_ready_dataset_image_tag, model_name),
        )


def _load(config_class, env):
    """Build one config class, naming every missing variable in one message."""
    env = os.environ if env is None else env
    missing = [name for name in config_class.REQUIRED if not env.get(name)]
    if missing:
        raise ConfigError(
            "missing required environment variable(s): " + ", ".join(missing)
        )
    return config_class(env)


def load(env=None):
    """Build a FeatureIngestionConfig from the process environment (or a mapping)."""
    return _load(FeatureIngestionConfig, env)


def load_cohort_label(env=None):
    """Build a CohortLabelConfig from the process environment (or a mapping)."""
    return _load(CohortLabelConfig, env)


def load_model_ready_dataset(env=None):
    """Build a ModelReadyDatasetConfig from the process environment (or a mapping)."""
    return _load(ModelReadyDatasetConfig, env)


def load_var(name, env=None):
    """One required variable.

    The stages after a compiler read every value they need from the compiled
    intake, so requiring the full config would make them fail on variables they
    never use.
    """
    env = os.environ if env is None else env
    value = env.get(name)
    if not value:
        raise ConfigError(f"missing required environment variable(s): {name}")
    return value
