"""naming.py — deterministic resource names derived from the model identity.

One place that turns model_name (+ environment) into the base job name, artifact
S3 prefix, Model Package Group name, and pipeline name. Keeping this centralized
means the same model always maps to the same resource names across runs.
"""

import re

from . import config

_MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,62}$")


def validate_model_name(model_name):
    """Lowercase, '_' or '-', starts with a letter, 3-63 chars. Raises ValueError."""
    if not _MODEL_NAME_RE.match(model_name or ""):
        raise ValueError(
            f"model_name '{model_name}' is invalid: must be lowercase alphanumeric "
            "with underscores or hyphens, start with a letter, and be 3-63 chars "
            "(^[a-z][a-z0-9_-]{2,62}$)."
        )
    return model_name


def slug(model_name):
    """Hyphenated form of `model_name` — SageMaker resource names allow hyphens,
    not underscores, and it's what `mrd_compiler.slug()` writes the MRD dataset's
    S3 path segment as. Public so a caller resolving that same S3 path (M5 reading
    the dataset M4 wrote) uses the identical transform rather than the raw name."""
    return model_name.replace("_", "-")


def dataset_dir_name(model_name):
    """The M4 dataset's FOLDER name under the MRD bucket.

    Slugged, because M4 slugs it when it writes: the model-ready-dataset compiler
    composes the dataset prefix as slug(model_name)/<s3_prefix>/<version>/ (see
    src/common/mrd_compiler.py dataset_s3_uri). Reading the raw model_name instead
    misses the folder entirely for any name carrying an underscore — every dataset
    in the dev bucket is hyphenated, while the manifest inside it records the
    underscored model_name, which is how the two can look consistent and not be.

    Distinct from dataset_name on TrainingConfig, which stays the model name: that
    field names the dataset, this one locates it.
    """
    return slug(model_name)


def base_job_name(model_name):
    """Job base name = the model identity (brand_model) only, no app/env prefix.

    model_name already carries the brand + model (e.g.
    brand_xyz_patient_outcome_mortality), so resource names are just its slug."""
    return slug(model_name)


def pipeline_name(model_name):
    return f"{slug(model_name)}-training"


# Our step names are the config's `compute` keys; the builder that pushes these
# images uses its own compute-agnostic stage words and names each repo
# <prefix><stage>. Translate here so the YAML never has to author the builder's
# vocabulary, and so a rename on either side fails in one place.
#
# `inference` is the odd one out in two ways: the two vocabularies already agree on
# the word (hence the identity entry), and it is the only stage that is NOT a
# pipeline step — nothing runs it here, the ref is recorded on the registered
# version as the container that will serve it. In practice nothing calls
# step_image_ref(.., "inference") today: the register step resolves the serving
# image through config.inference_image_ref() instead, which picks one of the two
# SHARED platform images (built by docs/runbooks/model-training/
# build_and_push_inference.sh) by CPU/GPU family — never a per-model derived repo.
# The entry stays here so the stage is a recognized name should a per-model
# inference image ever become a real thing.
_BUILDER_STAGES = {
    "preprocess": "preprocessing",
    "train": "training",
    "evaluate": "evaluation",
    "inference": "inference",
}


# ECR repository names are capped at 256 characters (AWS hard limit).
_MAX_REPO_NAME_LEN = 256


def _is_gpu_instance(instance_type):
    """True for GPU instance families (p*/g*, e.g. ml.g5.xlarge, ml.p3.2xlarge).

    Same family-parsing rule as config.inference_image_ref — kept in sync by
    hand since the two modules serve different image families (per-model
    train/eval/preprocess images here vs. the shared platform serving image
    there) and neither imports the other.
    """
    family = (instance_type or "").split(".")[1:2]  # 'ml.g5.xlarge' -> ['g5']
    return bool(family) and family[0][:1] in ("p", "g")


def step_image_ref(model_name, step, instance_type, tag="latest"):
    """Account-free 'repo:tag' for one step's container.

    The config no longer authors a registry location: a compute block names a
    repo-relative `dockerfile` (what to build) and the repo/tag is DERIVED here
    (where it lands), so the committed YAML stays account-free and two models can
    never collide on one repo. config.resolve_image_uri() prepends account+region.

    The derived name has to match what the builder actually pushed, or the step
    fails pulling an image that was never there: the builder names each per-model
    repo novos-ar-base-<cpu|gpu>-job-<stage>-<model-slug>, matching the platform's
    generic job-image vocabulary (novos-ar-base-<cpu|gpu>-job-<stage>) with the
    model identity appended, rather than a repo derived purely from the model.
    <cpu|gpu> comes from instance_type (see _is_gpu_instance) — unlike the old
    compute-agnostic ref, the two computes now name DIFFERENT repos, since the
    platform vocabulary they're matching already carries the compute split.

    The model name is NEVER dropped or abbreviated: if
    novos-ar-base-<cpu|gpu>-job-<stage>-<model-slug> would exceed ECR's 256-char
    repository name limit, characters are trimmed from the FRONT of the fixed
    prefix (novos-ar-base-...-job-<stage>-) rather than from model-slug — the
    model identity is what a human or another system greps for, so it survives
    intact even if the generic prefix has to give up characters to make room.

    Note nothing here BUILDS images — we only resolve this ref, so a matching
    image must already exist in ECR (pushed by the sample build_and_push.sh
    scripts). `dockerfile` / `local_requirements` are carried through to
    TrainingConfig for a later build step but are not acted on today.
    """
    if step not in _BUILDER_STAGES:
        raise ValueError(
            f"unknown step {step!r} — expected one of "
            f"{', '.join(sorted(_BUILDER_STAGES))}."
        )
    compute = "gpu" if _is_gpu_instance(instance_type) else "cpu"
    stage = _BUILDER_STAGES[step]
    model_slug = slug(model_name)
    prefix = f"novos-ar-base-{compute}-job-{stage}-"
    overflow = len(prefix) + len(model_slug) - _MAX_REPO_NAME_LEN
    if overflow > 0:
        prefix = prefix[overflow:]
    return f"{prefix}{model_slug}:{tag}"


def model_package_group(model_name):
    """Model Package Group name: DERIVED from the model identity. No app/env prefix.

    Not authorable. The config used to carry a registry.package_group that could
    redirect a model's versions into a group of another name; the current template
    drops it, so one model's versions always land in the group bearing its name and
    the group cannot drift from the model it holds.

    Slugged: SageMaker MPG names match ^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}, so an
    underscored name like brand_xyz_patient_outcome_mortality is REJECTED by
    CreateModelPackageGroup. We convert to hyphens here rather than failing at the
    API.
    """
    return slug(model_name)


# Leaf folder names for a run's step outputs. They nest under a run root
# (model_run_root/<exec-id>/) at pipeline BUILD time via the run-scoping helper in
# model_training.py; the non-scoped functions below (used for build-time-only
# pointers like the model card + Clarify's analysis-config) compose from the SAME
# constants so the folder vocabulary lives in exactly one place.
PREP_DIR = "prep"
EVAL_DIR = "evaluation"
BIAS_DIR = "bias"
OUTPUT_DIR = "output"
CONFIG_DIR = "config"


def model_run_key(model_name):
    """The per-model root as a BARE S3 KEY prefix (no scheme, no bucket).

    Same path as model_run_root without the s3://<bucket>/ head, for the few
    callers that need Bucket and Key as separate arguments (a boto3 put_object
    rather than an s3:// URI handed to SageMaker)."""
    return f"{config.ARTIFACT_PREFIX}/{model_name}"


def model_run_root(model_name):
    """s3:// per-model root under which EVERY run artifact lives.

    The pipeline interposes the execution id right after this (model_run_root/
    <exec-id>/) so each run gets ONE self-contained folder holding all of its step
    outputs (prep/evaluation/bias/output). The non-run-scoped functions below hang
    the leaf dirs directly off this root for the few build-time-only pointers that
    cannot carry a runtime execution id."""
    return f"s3://{config.ARTIFACT_BUCKET}/{model_run_key(model_name)}"


def prep_steps_key(model_name, digest):
    """S3 key of the preprocessing transform list the prep container reads.

    NOT run-scoped: the key carries a CONTENT DIGEST instead of the execution id,
    which the builder cannot know. That makes the object immutable — editing the
    YAML publishes a new key rather than rewriting the one an in-flight execution
    is about to download, and an unchanged config rewrites identical bytes to the
    same key. It sits under CONFIG_DIR, beside the run folders rather than inside
    one, because it is pipeline DEFINITION input: one object can be shared by
    every execution of the definition that references it."""
    return f"{model_run_key(model_name)}/{CONFIG_DIR}/preprocessing-steps-{digest}.json"


def bias_output_s3(model_name):
    """s3:// prefix where the Clarify bias step writes analysis.json + report.

    NO trailing slash: Clarify appends '/analysis_config.json' (and the other
    outputs) itself; a trailing '/' yields an illegal '//' key and the job fails
    at the first download. Non-run-scoped form; the pipeline run-scopes it under
    the execution id."""
    return f"{model_run_root(model_name)}/{BIAS_DIR}"
