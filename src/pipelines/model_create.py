"""model_create.py — M7 (CreateModel) seed: create a deployable SageMaker Model
from an APPROVED model package.

WHERE THIS FITS. M5 (model_training.py) trains + registers a Model PACKAGE
(metadata + InferenceSpecification) as PendingManualApproval. M6 is the approval
gate. M7 turns an APPROVED package into a deployable resource (an endpoint or a
Batch Transform runs a Model, NOT a package). This module does ONLY the first M7
action — CreateModel. It does not deploy an endpoint; a Model is inert until an
endpoint or a Batch Transform job is pointed at it (hence "create", not "deploy").

WHY IT LIVES IN src/ (not scripts/). This is written to be the importable core the
future approval Lambda calls — same split model_training.py uses (validate()/
launch() live here; scripts/ is only a thin dev CLI; nothing imports scripts/).
When the "create model on approval" Lambda is built, its handler unpacks the
ModelPackageArn from the EventBridge "SageMaker Model Package State Change" event
(ApprovalStatus -> Approved) and calls create_model_from_package(arn) — no logic
is duplicated into the Lambda.

DATAZONE TAGS ARE NON-NEGOTIABLE HERE. A Model created WITHOUT the AmazonDataZone*
tags does not appear in SageMaker Unified Studio's "Custom models -> Deployable"
list (that view is tag-filtered) — even though it exists and serves fine. So every
Model this creates carries config.datazone_tags() (+ BASE_TAGS), exactly like the
pipeline and the package group already do. (Confirmed the hard way: a hand-run CLI
create-model was invisible in Studio until the tags were added by hand.)

The Model is PACKAGE-BASED: Containers=[{"ModelPackageName": <arn>}] — the serving
image + ModelDataUrl come from the package's InferenceSpecification (the platform
serving image the register step wired in), so we never re-specify the image here.
"""

from src.model_training_core import config, naming


def latest_approved_package(model_package_group, *, session=None):
    """Return the ARN of the newest APPROVED version in a package group, or None.

    Convenience for the dev CLI (a human names the group, not a version). The
    approval Lambda does NOT use this — it is handed the exact ModelPackageArn by
    the approval event, so it never has to guess which version was approved.
    """
    sm = (session or config.session()).client("sagemaker")
    resp = sm.list_model_packages(
        ModelPackageGroupName=model_package_group,
        ModelApprovalStatus="Approved",
        SortBy="CreationTime",
        SortOrder="Descending",
        MaxResults=1,
    )
    pkgs = resp.get("ModelPackageSummaryList", [])
    return pkgs[0]["ModelPackageArn"] if pkgs else None


def _model_name_from_package(desc):
    """Deterministic SageMaker Model name = <group-slug>-v<version>.

    The group name is already a hyphen slug (naming.model_package_group); the
    package version disambiguates re-creates of the same logical model. Stays well
    under the 63-char Model-name limit for our brand_model identities.
    """
    group = desc["ModelPackageGroupName"]
    version = desc.get("ModelPackageVersion")
    return f"{group}-v{version}" if version is not None else group


def _tag_list():
    """BASE_TAGS + DataZone tags as CreateModel Tags ([{Key,Value}]).

    DataZone tags are what make the Model show under the AR project in Unified
    Studio's Deployable list; omitted only when the env vars are unset (then the
    Model still exists, just in classic Studio). Mirrors pipeline_tags() in
    model_training.py."""
    merged = dict(config.BASE_TAGS)
    merged.update(config.datazone_tags())
    return [{"Key": k, "Value": str(v)} for k, v in merged.items()]


def create_model_from_package(
    model_package_arn,
    *,
    model_name=None,
    role=None,
    session=None,
    require_approved=True,
):
    """Create a deployable SageMaker Model from a registered model package.

    This is the function the approval Lambda calls. It is idempotent: if the Model
    name already exists it returns the existing one rather than failing (a replayed
    approval event must not error).

    Args:
      model_package_arn: the versioned package ARN to create a Model from (from the
          approval event, or resolved by latest_approved_package for the CLI).
      model_name: Model resource name; defaults to <group>-v<version>.
      role: execution role the Model runs as; defaults to config.execution_role_arn().
      session: boto3 Session; defaults to config.session().
      require_approved: refuse to create from a package that is not Approved
          (guards a mis-wired trigger). Set False only for a dev dry-run against a
          Pending package.

    Returns: {"model_name", "model_package_arn", "created"} (created=False when the
      Model already existed).
    """
    sess = session or config.session()
    sm = sess.client("sagemaker")

    desc = sm.describe_model_package(ModelPackageName=model_package_arn)
    approval = desc.get("ModelApprovalStatus")
    if require_approved and approval != "Approved":
        raise RuntimeError(
            f"model package is '{approval}', not 'Approved' — refusing to create a "
            f"deployable Model. ({model_package_arn})"
        )

    model_name = model_name or _model_name_from_package(desc)
    role = role or config.execution_role_arn()
    tags = _tag_list()

    try:
        sm.create_model(
            ModelName=model_name,
            # Package-based: image + ModelDataUrl come from the package's
            # InferenceSpecification (the platform serving image the register step
            # wired in). We deliberately do NOT set PrimaryContainer.Image here.
            Containers=[{"ModelPackageName": model_package_arn}],
            ExecutionRoleArn=role,
            Tags=tags,
        )
        print(f"[m7  ] created Model '{model_name}' from {model_package_arn}")
        print(f"[m7  ] tags: {[t['Key'] for t in tags]} (role={role})")
        created = True
    except sm.exceptions.ClientError as e:
        # Idempotent: a replayed approval event (or a re-run CLI) must not fail.
        if "already exist" not in str(
            e
        ).lower() and "Cannot create already existing" not in str(e):
            raise
        print(f"[m7  ] Model '{model_name}' already exists — reusing (idempotent)")
        created = False

    return {
        "model_name": model_name,
        "model_package_arn": model_package_arn,
        "created": created,
    }


def create_model_group_latest(
    model_name_or_group, *, model_name=None, role=None, session=None
):
    """CLI convenience: resolve a group's latest APPROVED package, then create it.

    Accepts either the model identity (brand_model, snake_case — turned into the
    group slug via naming) or an already-slugged group name. The approval Lambda
    bypasses this and calls create_model_from_package with the event's exact ARN.
    """
    sess = session or config.session()
    # Accept a snake_case model identity OR a ready group slug (idempotent slug()).
    group = (
        naming.model_package_group(model_name_or_group)
        if "_" in model_name_or_group
        else model_name_or_group
    )
    arn = latest_approved_package(group, session=sess)
    if not arn:
        raise RuntimeError(
            f"no Approved model package found in group '{group}' — approve a version "
            "(M6) before creating a Model, or pass an explicit --model-package-arn."
        )
    print(f"[m7  ] latest Approved in '{group}': {arn}")
    return create_model_from_package(
        arn, model_name=model_name, role=role, session=sess
    )
