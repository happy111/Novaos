#!/usr/bin/env python3
"""AR Platform CDK app- env-driven, SSM-wired."""

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import DefaultStackSynthesizer
from cdk_stacks.cdk_cohort_label import CdkCohortLabelStack
from cdk_stacks.cdk_feature_onboarding import CdkFeatureOnboardingStack
from cdk_stacks.cdk_iam_deploy_roles import CdkIamDeployRolesStack
from cdk_stacks.cdk_lambdas import CdkLambdasStack
from cdk_stacks.cdk_model_ready_dataset import CdkModelReadyDatasetStack
from cdk_stacks.cdk_model_training_state_machine import (
    CdkModelTrainingStateMachineStack,
)
from cdk_stacks.cdk_notifications import CdkNotificationsStack
from cdk_stacks.cdk_s3_buckets import CdkS3BucketsStack
from cdk_stacks.cdk_tagging_aspect import CdkTaggingAspect

app = cdk.App()

# ── Load env config from environments/*.json ─────────────────────
environment = app.node.try_get_context("environment") or "dev"
config_path = Path(__file__).parent / "environments" / f"{environment}.json"
if not config_path.exists():
    raise FileNotFoundError(f"Config not found: {config_path}")

with open(config_path) as f:
    config = {environment: json.load(f)}

var_config = config[environment]
stack_prefix = var_config["stack_prefix"]
app_prefix = var_config["app_prefix"]
account = var_config["account"]
region = var_config["region"]
roles = var_config.get("roles", {})

appadm_role = roles.get("appadm_role", "")
dpadmin_role = roles.get("dpadmin_role", "")
cdk_lookup_role = roles.get("cdk_lookup_role", "")
cdk_file_asset_publishing_role = roles.get("cdk_file_asset_publishing_role", "")
cdk_image_asset_publishing_role = roles.get("cdk_image_asset_publishing_role", "")

cdk_env = cdk.Environment(account=account, region=region)

# ── DefaultStackSynthesizer — DDIT-provided CDK execution roles ──
synth = DefaultStackSynthesizer(
    deploy_role_arn=f"arn:aws:iam::{account}:role/{appadm_role}",
    cloud_formation_execution_role=f"arn:aws:iam::{account}:role/{appadm_role}",
    lookup_role_arn=f"arn:aws:iam::{account}:role/{cdk_lookup_role}",
    file_asset_publishing_role_arn=f"arn:aws:iam::{account}:role/{cdk_file_asset_publishing_role}",
    image_asset_publishing_role_arn=f"arn:aws:iam::{account}:role/{cdk_image_asset_publishing_role}",
)

synth_dp_admin = DefaultStackSynthesizer(
    deploy_role_arn=f"arn:aws:iam::{account}:role/{dpadmin_role}",
    cloud_formation_execution_role=f"arn:aws:iam::{account}:role/{dpadmin_role}",
    lookup_role_arn=f"arn:aws:iam::{account}:role/{cdk_lookup_role}",
    file_asset_publishing_role_arn=f"arn:aws:iam::{account}:role/{cdk_file_asset_publishing_role}",
    image_asset_publishing_role_arn=f"arn:aws:iam::{account}:role/{cdk_image_asset_publishing_role}",
)

# ── Stack 0: IAM Roles ───────────────────────────────────────────
iam_roles = CdkIamDeployRolesStack(
    scope=app,
    id=f"{stack_prefix}-IamRoles-{app_prefix}-{environment}",
    synthesizer=synth,
    environment=environment,
    config=config,
    env=cdk_env,
)

s3_buckets = CdkS3BucketsStack(
    scope=app,
    id=f"{stack_prefix}-S3Buckets-{app_prefix}-{environment}",
    synthesizer=synth,
    environment=environment,
    config=config,
    env=cdk_env,
)

# ── Stack 2: Notifications (SNS topics + SQS queues) ─────────────
notifications = CdkNotificationsStack(
    scope=app,
    id=f"{stack_prefix}-Notifications-{app_prefix}-{environment}",
    synthesizer=synth,
    environment=environment,
    config=config,
    env=cdk_env,
)
# notifications.add_dependency(iam_roles)

# ── Stack: Lambdas ───────────────────────────────────────────────
lambdas = CdkLambdasStack(
    scope=app,
    id=f"{stack_prefix}-Lambdas-{app_prefix}-{environment}",
    synthesizer=synth,
    environment=environment,
    config=config,
    env=cdk_env,
)
if environment != "dev":
    # Dev is deployed/iterated on frequently and its IamRoles/S3Buckets are
    # already stable, so this ordering isn't enforced there. QA/prod deploys
    # matter more for correctness, so force IamRoles/S3Buckets first — Lambda
    # env vars read S3 bucket names/URIs from SSM (value_for_string_parameter
    # resolves at deploy), so the S3 stack must publish them first there.
    lambdas.add_dependency(iam_roles)
    lambdas.add_dependency(s3_buckets)

# ── Stack: Feature onboarding state machine ──────────────────────
feature_onboarding = CdkFeatureOnboardingStack(
    scope=app,
    # Kept as -StateMachine- : this is the deployed CloudFormation stack name, and
    # changing it orphans the stack rather than renaming it.
    id=f"{stack_prefix}-StateMachine-{app_prefix}-{environment}",
    synthesizer=synth,
    environment=environment,
    config=config,
    env=cdk_env,
)
# The role comes from IamRoles; the two invoked functions from Lambdas.
if environment != "dev":
    feature_onboarding.add_dependency(iam_roles)
    feature_onboarding.add_dependency(lambdas)

# ── Stack: Cohort-and-label state machine (AR-18-a-2) ────────────
cohort_label = CdkCohortLabelStack(
    scope=app,
    id=f"{stack_prefix}-CohortLabel-{app_prefix}-{environment}",
    synthesizer=synth,
    environment=environment,
    config=config,
    env=cdk_env,
)
# The role comes from IamRoles; the invoked validator function from Lambdas.
if environment != "dev":
    cohort_label.add_dependency(iam_roles)
    cohort_label.add_dependency(lambdas)

# ── Stack: Model-ready-dataset state machine (AR-18-b-2) ─────────
model_ready_dataset = CdkModelReadyDatasetStack(
    scope=app,
    id=f"{stack_prefix}-ModelReadyDataset-{app_prefix}-{environment}",
    synthesizer=synth,
    environment=environment,
    config=config,
    env=cdk_env,
)
# The role comes from IamRoles; the invoked validator function from Lambdas.
if environment != "dev":
    model_ready_dataset.add_dependency(iam_roles)
    model_ready_dataset.add_dependency(lambdas)

# ── Stack: Model-training state machine ──────────────────────────
model_training_state_machine = CdkModelTrainingStateMachineStack(
    scope=app,
    id=f"{stack_prefix}-ModelTrainingStateMachine-{app_prefix}-{environment}",
    synthesizer=synth,
    environment=environment,
    config=config,
    env=cdk_env,
)
# The role comes from IamRoles; the two invoked functions from Lambdas.
if environment != "dev":
    model_training_state_machine.add_dependency(iam_roles)
    model_training_state_machine.add_dependency(lambdas)

# ── Mandatory cost-allocation tagging  ─────────────
# Aspect validates all 6 required tags at synth time and applies them
resolved_tags = {
    key: value.replace("{app_prefix}", app_prefix) if isinstance(value, str) else value
    for key, value in var_config.get("tags", {}).items()
}
cdk.Aspects.of(app).add(CdkTaggingAspect(resolved_tags, environment))

app.synth()
