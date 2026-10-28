"""Shared boilerplate for the AR state-machine stacks.

The cohort-label, feature-onboarding and model-training stacks each end with the
same tail: resolve the execution role by name, create the vended-logs log group,
build the Standard state machine with ALL-level logging, and publish the ARN +
log-group name to SSM. `add_state_machine` is that tail, factored out once.

Logical IDs are driven by `id_prefix` (and `ssm_prefix` for the SSM parameter
names/descriptions) so each caller keeps the exact construct IDs it had before —
synth output is unchanged, no CloudFormation resource is replaced.
"""

from aws_cdk import Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct


def add_state_machine(
    scope: Construct,
    *,
    id_prefix: str,
    state_machine_name: str,
    definition: sfn.IChainable,
    role_config: str,
    account_role_prefix: str,
    app_prefix: str,
    environment: str,
    ssm_prefix: str,
    timeout: Duration,
    comment: str,
    log_retention: str,
) -> sfn.StateMachine:
    """Create the role/log-group/state-machine/SSM tail shared by the SM stacks.

    `id_prefix` is used verbatim as the StateMachine construct id and as the prefix
    for the role/log-group/SSM construct ids (e.g. "StateMachine" ->
    "StateMachineRole"), so each caller preserves its existing logical IDs.
    """
    role_name = role_config.replace(
        "{account_role_prefix}", account_role_prefix
    ).replace("{app_prefix}", app_prefix)
    role = iam.Role.from_role_name(scope, f"{id_prefix}Role", role_name, mutable=False)

    log_group = logs.LogGroup(
        scope,
        f"{id_prefix}LogGroup",
        log_group_name=f"/aws/vendedlogs/states/{state_machine_name}",
        # A Standard workflow is given no retention unless it is set here (V28).
        retention=logs.RetentionDays(log_retention),
    )

    state_machine = sfn.StateMachine(
        scope,
        id_prefix,
        state_machine_name=state_machine_name,
        state_machine_type=sfn.StateMachineType.STANDARD,
        definition_body=sfn.DefinitionBody.from_chainable(definition),
        role=role,
        comment=comment,
        timeout=timeout,
        # A Standard workflow logs nothing at all without this (AC-6 / V28).
        logs=sfn.LogOptions(
            destination=log_group,
            level=sfn.LogLevel.ALL,
            include_execution_data=True,
        ),
    )

    ssm.StringParameter(
        scope,
        f"{id_prefix}Arn",
        parameter_name=f"/{app_prefix}/{environment}/state-machine/{ssm_prefix}-arn",
        string_value=state_machine.state_machine_arn,
        description=f"ARN of the {ssm_prefix} state machine",
    )
    ssm.StringParameter(
        scope,
        f"{id_prefix}LogGroupName",
        parameter_name=f"/{app_prefix}/{environment}/state-machine/{ssm_prefix}-log-group",
        string_value=log_group.log_group_name,
        description=f"CloudWatch log group of the {ssm_prefix} state machine",
    )
    return state_machine
