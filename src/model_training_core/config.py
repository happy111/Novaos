"""config.py — infrastructure config (NO model/feature values).

Holds only "where it runs": region, the artifact/report bucket, the execution
role, the CMK, and the default dataset location. Every value comes from the
environment (env vars today; later, cdk_pipeline/environments/<env>.json +
SSM). Per the repo's structure rules, account IDs, regions, roles, and CMKs
must NOT be hardcoded in Python source.

Resource NAMES (pipeline, job, model package group) derive purely from the model
identity (brand_model) in naming.py — there is no app/environment prefix.

ALL model-specific config (model name, image, hyperparameters, datasets, target)
comes from model-training-config.yaml via training_config.py. A new model onboards
by dropping a YAML; nobody edits this file.

KMS note: the platform wraps training artifacts with a CMK. The CMK ARN is
resolved from config (env var today; SSM param /{app_prefix}/{env}/kms-key-arn
in the CDK world). If unset, we fall back to SSE-S3 (SageMaker default), matching
what the feature-store sample did — see that sample's config.py.
"""

import os

import boto3


def _require(name):
    """Read a REQUIRED infra var from the environment; fail fast if unset.

    Per the repo's structure rules, account-specific infra (account IDs, region,
    roles, CMKs, buckets) is NEVER hardcoded in source — no fallback defaults.
    Values come from the environment; copy .env.example to .env (gitignored),
    fill it in, and `source` it before running.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"required environment variable {name} is not set — copy .env.example "
            "to .env, fill it in, then: set -a; source .env; set +a"
        )
    return value


def _optional(name, default=""):
    """Read an OPTIONAL var; treat present-but-empty the same as absent.

    A .env copied from .env.example carries empty keys (AR_FOO=), so an empty
    value must fall back to `default` just like a missing key — otherwise the
    documented default never applies.
    """
    return os.environ.get(name, "").strip() or default


# --- Environment (resolved on FIRST ACCESS, not at import) ---
# Each entry maps the module attribute to how it is read. Resolution is deferred
# via __getattr__ below so that importing this module — and therefore importing
# training_config, naming and validation — needs NO environment at all. The
# schema/compiler layer must be unit-testable without AWS; reading
# env at import time made `import training_config` raise RuntimeError offline.
# Callers still write config.REGION / config.ARTIFACT_BUCKET unchanged; the
# fail-fast RuntimeError just moves to the first read, i.e. to code that actually
# needs infra.
_ENV = {
    "REGION": lambda: _require("AWS_REGION"),
    # Artifact + report bucket (account-specific -> required). This is BOTH the
    # input dataset bucket (datasets.py resolves the M4 dataset here) and the
    # bucket the pipeline writes its own outputs (prep/eval/bias/model artifacts) to.
    "ARTIFACT_BUCKET": lambda: _require("AR_ARTIFACT_BUCKET"),
    # Prefix under the bucket. Training artifacts land under ARTIFACT_PREFIX
    # (the model name is appended per-resource by naming.py, so no app/env prefix
    # here); the M4 dataset's S3 prefix WITHIN the dataset bucket is authored in
    # the YAML (data.source.s3_prefix) — see dataset_bucket_name() for the
    # dataset BUCKET itself, which is a separate, derived name.
    "ARTIFACT_PREFIX": lambda: _optional("AR_ARTIFACT_PREFIX", "model-training"),
    # Environment + app prefix: the two variable components of the dataset
    # bucket's name (see dataset_bucket_name()). Not used anywhere else —
    # resource NAMES elsewhere derive purely from model identity (naming.py).
    #
    # Read UNPREFIXED, unlike the AR_* vars around them: ENV and APP_PREFIX are the
    # platform-wide names every `lambdas` block in cdk_pipeline/environments/<env>.json
    # already sets, so the two pipeline Lambdas carry them with nothing added per
    # pipeline. An AR_-prefixed pair would duplicate values already in the
    # environment, and while these read them the state machine failed here on every
    # execution — the Lambdas have never carried AR_ENVIRONMENT or AR_APP_PREFIX.
    "ENVIRONMENT": lambda: _require("ENV"),
    "APP_PREFIX": lambda: _require("APP_PREFIX"),
    # CMK ARN for artifact encryption (processing/training instance storage + S3
    # uploads). Consumed at PROVISIONING time (needs kms:CreateGrant), so a
    # bucket-default CMK that the job can't grant on will hang provisioning.
    # Empty -> SSE default. Keep this empty unless the execution role is
    # confirmed to hold CreateGrant on the key.
    "KMS_KEY_ARN": lambda: _optional("AR_KMS_KEY_ARN"),
    # Execution role for the training job / pipeline. Empty -> resolve the
    # caller's role at runtime (Studio execution role), which is the default.
    "EXECUTION_ROLE_ARN": lambda: _optional("AR_EXECUTION_ROLE_ARN"),
    # SageMaker Unified Studio (DataZone) placement. A pipeline / model package
    # surfaces inside a Unified Studio PROJECT only when it carries the DataZone
    # domain + project tags — there is no separate API, it is purely tag-driven.
    # Both are account-specific -> env vars (never hardcode infra). When
    # unset, these tags are simply omitted (resources still exist in classic Studio).
    "DATAZONE_DOMAIN_ID": lambda: _optional(
        "AR_DATAZONE_DOMAIN_ID"
    ),  # dzd-c3poc7rfn8nyw7
    "DATAZONE_PROJECT_ID": lambda: _optional(
        "AR_DATAZONE_PROJECT_ID"
    ),  # do9axtshgp150n
    # VPC placement for the Processing/Training jobs the Lambda/state-machine path
    # runs as (RRDRAR65DEV_AWS_APP01-novos-ar-workload-pipelineexecution). Optional:
    # the documented local path (scripts/run_training_pipeline.py, this session's
    # own broadly-permissioned role) runs with NO VPC at all — confirmed against a
    # real, successful execution (brand-abc-mortality-v2-stress-training) — so a
    # blank value here must mean "no VPC config", not a hard failure. Comma-
    # separated, no spaces, when set (matches every sibling pipeline's CDK stack
    # reading network.security_groups.lambda / network.subnet_groups.routable from
    # cdk_pipeline/environments/<env>.json).
    "VPC_SECURITY_GROUP_IDS": lambda: [
        v.strip()
        for v in _optional("AR_VPC_SECURITY_GROUP_IDS").split(",")
        if v.strip()
    ],
    "VPC_SUBNET_IDS": lambda: [
        v.strip() for v in _optional("AR_VPC_SUBNET_IDS").split(",") if v.strip()
    ],
}


def __getattr__(name):
    """Resolve an infra constant on first access (PEP 562 module __getattr__).

    Only consulted for names NOT already in the module namespace, so a resolved
    value is cached back into globals() and every later read is a plain lookup.
    """
    try:
        value = _ENV[name]()
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    globals()[name] = value
    return value


def _v(name):
    """Read an infra constant from INSIDE this module.

    Module-level __getattr__ fires only for attribute access from outside
    (config.REGION); a bare global reference in a function body here would raise
    NameError instead, because the name is genuinely absent from globals() until
    first resolution. Functions below therefore go through _v().
    """
    if name in globals():
        return globals()[name]
    return __getattr__(name)


def reset_env_cache():
    """Forget resolved infra constants so the next access re-reads os.environ.

    For tests that set/clear AR_* vars between cases; without this the first
    resolution would be cached for the whole session.
    """
    for name in _ENV:
        globals().pop(name, None)


# Base tags applied to every created resource (merged with per-model tags).
BASE_TAGS = {
    "managed_by": "ar-model-training-pipeline",
}


def datazone_tags():
    """Unified Studio (DataZone) visibility tags, or {} when not configured.

    Stamped onto the pipeline (and inherited by the registered model package)
    so both appear under the AR project in SageMaker Unified Studio. Only emitted
    when BOTH domain and project are set — a lone tag does nothing and would only
    add noise. The AR Unified Studio example uses domain+project (no ScopeName)."""
    domain, project = _v("DATAZONE_DOMAIN_ID"), _v("DATAZONE_PROJECT_ID")
    if domain and project:
        return {
            "AmazonDataZoneDomain": domain,
            "AmazonDataZoneProject": project,
        }
    return {}


def vpc_network_config():
    """The account's VPC placement as `sagemaker.core.network.NetworkConfig`, or
    None when AR_VPC_SECURITY_GROUP_IDS/AR_VPC_SUBNET_IDS are unset.

    For every `Processor` (prep/evaluate) and the Clarify processor: all three
    resolve `network_config` through this same shape. Imported locally (not at
    module level) so importing config.py — and therefore training_config.py,
    naming.py, validation.py — needs no sagemaker SDK install, matching this
    module's existing AWS-free-until-first-use design.
    """
    security_groups, subnets = _v("VPC_SECURITY_GROUP_IDS"), _v("VPC_SUBNET_IDS")
    if not security_groups or not subnets:
        return None

    from sagemaker.core.network import NetworkConfig

    return NetworkConfig(security_group_ids=security_groups, subnets=subnets)


def vpc_networking():
    """The same VPC placement as `sagemaker.core.training.configs.Networking`,
    the shape `ModelTrainer` (the Train step) takes instead of `NetworkConfig`.
    None when unset, same as `vpc_network_config()`."""
    security_groups, subnets = _v("VPC_SECURITY_GROUP_IDS"), _v("VPC_SUBNET_IDS")
    if not security_groups or not subnets:
        return None

    from sagemaker.core.training.configs import Networking

    return Networking(security_group_ids=security_groups, subnets=subnets)


def session():
    """A boto3 Session bound to the configured region."""
    return boto3.Session(region_name=_v("REGION"))


def account_id():
    return session().client("sts").get_caller_identity()["Account"]


def execution_role_arn():
    """Resolve the execution role ARN.

    Uses AR_EXECUTION_ROLE_ARN when set; otherwise resolves the caller's role
    (expanding an assumed-role STS ARN to the underlying IAM role ARN, so any
    /service-role/ path is preserved). Mirrors the feature-store sample.
    """
    explicit = _v("EXECUTION_ROLE_ARN")
    if explicit:
        return explicit
    sess = session()
    arn = sess.client("sts").get_caller_identity()["Arn"]
    if ":assumed-role/" in arn:
        name = arn.split(":assumed-role/")[1].split("/")[0]
        return sess.client("iam").get_role(RoleName=name)["Role"]["Arn"]
    return arn


def dataset_bucket_name():
    """The MRD (model-ready-dataset) bucket name, derived from account + env.

    novartis-{account_id}-{environment}-{region}-{app_prefix}-mrd-datasets —
    a fixed platform naming convention, not authored anywhere per-model. The
    dataset itself then sits at <bucket>/<model_name>/<data.source.s3_prefix>/
    <version>/ (see datasets.py).
    """
    return (
        f"novartis-{account_id()}-{_v('ENVIRONMENT')}-{_v('REGION')}-"
        f"{_v('APP_PREFIX')}-mrd-datasets"
    )


def resolve_image_uri(image_ref):
    """Turn an account-free 'repo:tag' into a full ECR URI.

    The YAML never names a registry: the committed config stays account-free and
    the per-step repo:tag is derived from the model identity by
    naming.step_image_ref(). The platform prepends account + region here, at
    runtime. A value that already looks like a full URI (contains '.dkr.ecr.') is
    returned unchanged. Mirrors default_image_uri() in the feature-store sample.
    """
    if not image_ref:
        raise ValueError("resolved image reference is empty")
    if ".dkr.ecr." in image_ref:
        return image_ref
    return f"{account_id()}.dkr.ecr.{_v('REGION')}.amazonaws.com/{image_ref}"


# The two shared platform SERVING images (built by
# docs/runbooks/model-training/build_and_push_inference.sh, src/images/
# job-inference-cpu and job-inference-gpu). Named per the tier-1 job-base
# convention the rest of the per-model generic images use
# (novos-ar-base-<cpu|gpu>-job-<stage>) — same vocabulary as
# novos-ar-base-cpu-job-training, just for the "inference" stage. Unlike
# prep/train/evaluate — where each model declares its own dedicated image —
# inference bakes NO model code (the model's serving code rides in
# model.tar.gz's code/), so ONE image per compute serves EVERY model. The
# register step points the InferenceSpecification at whichever matches the
# serving hardware; the account/region are prepended by resolve_image_uri
# like any other ref.
INFERENCE_IMAGE_REPO_CPU = "novos-ar-base-cpu-job-inference"
INFERENCE_IMAGE_REPO_GPU = "novos-ar-base-gpu-job-inference"


def inference_image_ref(instance_type):
    """Pick the platform serving image (account-free repo:tag) for an instance type.

    GPU instance families (p*/g*, e.g. ml.g5.xlarge, ml.p3.2xlarge) serve on the
    CUDA image; everything else on the CPU image. Returns a 'repo:latest' ref that
    resolve_image_uri() expands to a full ECR URI. The register step derives the
    serving image from the training instance family so no YAML change is needed —
    both images are model-agnostic, so any compatible model.tar.gz serves on either.
    """
    family = (instance_type or "").split(".")[1:2]  # 'ml.g5.xlarge' -> ['g5']
    is_gpu = bool(family) and family[0][:1] in ("p", "g")
    repo = INFERENCE_IMAGE_REPO_GPU if is_gpu else INFERENCE_IMAGE_REPO_CPU
    return f"{repo}:latest"
