"""Tests for CdkFeatureOnboardingStack."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from cdk_stacks.cdk_feature_onboarding import CdkFeatureOnboardingStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
APP_PREFIX = "novos-ar"
SUBNETS = ["subnet-0537c8dda74f4723b", "subnet-0811c3073b6ae7f26"]
SECURITY_GROUPS = ["sg-0fff0e9160460002d"]

# No image, instance or role here on purpose: those come from stage 1's return
# value, so only what the intake form has no field for is configured.
INGEST = {
    "volume_size_gb": 30,
    "max_runtime_seconds": 5400,
    "subnet_group": "routable",
    "security_group": "lambda",
    "env_vars": {"ENV": "{env}", "ACCOUNT_ID": ACCOUNT, "AWS_REGION": REGION},
}

STATE_MACHINE = {
    "name": "feature-onboarding",
    "description": "NovaOS AR feature onboarding",
    "role": "{account_role_prefix}{app_prefix}-statemachine-feature-onboarding",
    "compiler_lambda": "feature-intake-compiler",
    "creater_lambda": "feature-group-creater",
    "wait_seconds": 30,
    "timeout_minutes": 120,
    "log_retention": "ONE_MONTH",
    "ingest": INGEST,
}

SNOWFLAKE = {
    "env_vars": {
        "SNOWFLAKE_ACCOUNT": "nvs2deveast1.us-east-1.privatelink",
        "SNOWFLAKE_HOST": "nvs2deveast1.us-east-1.privatelink.snowflakecomputing.com",
        "SNOWFLAKE_USER_SECRET_ID": "f1ai-pwsa0002037",
        "SNOWFLAKE_PRIVATE_KEY_SECRET_ID": "f1ai-pwsa0002037-key",
        "NO_PROXY": "snowflakecomputing.com",
    }
}


def _state_machine_config(environment: str, state_machine: dict = None) -> dict:
    config = {
        environment: {
            "app_prefix": APP_PREFIX,
            "account": ACCOUNT,
            "region": REGION,
            "account_role_prefix": f"RRDRAR65{environment.upper()}_AWS_APP01-",
            "network": {
                "subnet_groups": {"routable": SUBNETS},
                "security_groups": {"lambda": SECURITY_GROUPS},
            },
            "snowflake": SNOWFLAKE,
            "tags": {"ClarityID": "96354", "Environment": environment},
        }
    }
    if state_machine is not None:
        config[environment]["state_machines"] = {"feature_onboarding": state_machine}
    return config


def _template(state_machine: dict = None) -> Template:
    stack = CdkFeatureOnboardingStack(
        scope=cdk.App(),
        id="TestStateMachine",
        environment="dev",
        config=_state_machine_config("dev", state_machine),
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack)


@pytest.fixture
def sm_template():
    return _template(STATE_MACHINE)


@pytest.fixture
def definition(sm_template):
    """The rendered ASL. Fn::Join parts that are tokens are the partition."""
    machines = sm_template.find_resources("AWS::StepFunctions::StateMachine")
    assert len(machines) == 1
    parts = list(machines.values())[0]["Properties"]["DefinitionString"]["Fn::Join"][1]
    asl = "".join(p if isinstance(p, str) else "aws" for p in parts)
    return json.loads(asl)


def test_state_machine_created(sm_template):
    sm_template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {
            "StateMachineName": f"{APP_PREFIX}-feature-onboarding-dev",
            "StateMachineType": "STANDARD",
        },
    )
    # The role is created by CdkIamDeployRolesStack and imported by name here.
    machines = sm_template.find_resources("AWS::StepFunctions::StateMachine")
    role = str(list(machines.values())[0]["Properties"]["RoleArn"])
    assert f"RRDRAR65DEV_AWS_APP01-{APP_PREFIX}-statemachine-feature-onboarding" in role


def test_the_definition_needs_no_branch_per_input_shape(definition):
    """One Pass adds the minted run id beside the input; stage 1 resolves the rest.

    ASL cannot reference an absent path, so deciding the input shape here would cost
    a Choice and a Pass for every combination of optional field. Deciding it in the
    handler keeps this to a single state.
    """
    assert definition["StartAt"] == "MintRunId"
    states = definition["States"]

    mint = states["MintRunId"]
    assert mint["Type"] == "Pass"
    # ResultPath ADDS beside the input rather than replacing it, which is what lets
    # both input shapes through without either being named.
    assert mint["ResultPath"] == "$.platform"
    assert set(mint["Parameters"]) == {"generated_run_id.$"}
    assert mint["Next"] == "Compile"

    # Nothing resolves the input shape in the definition any more.
    assert not [name for name in states if "Resolve" in name and "Ingest" not in name]
    assert "$.model_id" not in json.dumps(definition)


def test_a_generated_run_id_is_twelve_characters(definition):
    """ASL has no substring intrinsic, so the 12-char UUID group is taken whole.

    Both Lambdas enforce ^r[a-z0-9]{12}$, which is 12 and not the 11 in SPEC §6.3.
    """
    generated = definition["States"]["MintRunId"]["Parameters"]["generated_run_id.$"]
    assert generated == (
        "States.Format('r{}', "
        "States.ArrayGetItem(States.StringSplit(States.UUID(), '-'), 4))"
    )


def test_the_stages_are_invoked_in_order_with_their_contract(definition):
    states = definition["States"]

    compile_stage = states["Compile"]
    assert (
        compile_stage["Parameters"]["FunctionName"]
        == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{APP_PREFIX}-feature-intake-compiler-dev"
    )
    # The whole execution input reaches stage 1, so no input field is named here.
    assert compile_stage["Parameters"]["Payload.$"] == "$"
    # The compiled intake is the contract between stages, so each stage's return
    # value is the next one's input.
    assert compile_stage["OutputPath"] == "$.Payload"
    assert compile_stage["Next"] == "CreateGroup"

    create = states["CreateGroup"]
    assert (
        create["Parameters"]["FunctionName"]
        == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{APP_PREFIX}-feature-group-creater-dev"
    )
    assert create["Parameters"]["Payload"] == {
        "run_id.$": "$.run_id",
        "compiled_intake_s3_uri.$": "$.compiled_intake_s3_uri",
        "action": "create",
        "recreate.$": "$.recreate",
    }
    assert create["Next"] == "DescribeFeatureGroup"


def test_the_wait_loop_polls_describe_feature_group(definition):
    """AR-05-d AC-7: the group's status has to reach Created before anything else."""
    states = definition["States"]

    describe = states["DescribeFeatureGroup"]
    # A native aws-sdk integration, not a third Lambda: it runs in the Step
    # Functions service, so it needs no SageMaker VPC endpoint.
    assert (
        describe["Resource"]
        == "arn:aws:states:::aws-sdk:sagemaker:describeFeatureGroup"
    )
    assert describe["Parameters"] == {"FeatureGroupName.$": "$.feature_group_name"}
    # The whole describe response is kept beside the create output — that is the
    # AC-7 evidence in the console: both stores, the CMK on each, the status.
    assert describe["ResultPath"] == "$.describe"
    assert describe["Next"] == "CheckFeatureGroupStatus"

    assert states["CheckFeatureGroupStatus"]["Choices"] == [
        {
            "Variable": "$.describe.FeatureGroupStatus",
            "StringEquals": "Created",
            "Next": "Document",
        },
        {
            "Variable": "$.describe.FeatureGroupStatus",
            "StringEquals": "Creating",
            "Next": "WaitForCreated",
        },
    ]
    assert states["WaitForCreated"] == {
        "Type": "Wait",
        "Seconds": 30,
        "Next": "DescribeFeatureGroup",
    }


def test_a_failed_creation_fails_the_execution_with_the_reason(definition):
    """A CreateFailed group cannot be repaired, so the reason is the whole point."""
    states = definition["States"]
    assert states["CheckFeatureGroupStatus"]["Default"] == "FeatureGroupCreateFailed"
    assert states["FeatureGroupCreateFailed"] == {
        "Type": "Fail",
        "Error": "FeatureGroupCreateFailed",
        "CausePath": "$.describe.FailureReason",
    }


def test_the_features_are_documented_once_the_group_is_created(definition):
    """AR-05-d AC-4: UpdateFeatureMetadata only applies to a group that is Created.

    Without this second invocation of stage 2 no feature description ever reaches
    the store, because CreateFeatureGroup cannot carry one.
    """
    document = definition["States"]["Document"]
    assert (
        document["Parameters"]["FunctionName"]
        == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{APP_PREFIX}-feature-group-creater-dev"
    )
    assert document["Parameters"]["Payload"] == {
        "run_id.$": "$.run_id",
        "compiled_intake_s3_uri.$": "$.compiled_intake_s3_uri",
        "action": "document",
    }
    # Documenting is no longer terminal: stage 4 ingests once it has run.
    assert document["Next"] == "ResolveIngestJob"


def test_execution_data_is_logged_with_an_explicit_retention(sm_template):
    """V28: a Standard workflow logs nothing unless all three are set."""
    sm_template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {
            "LoggingConfiguration": {
                "IncludeExecutionData": True,
                "Level": "ALL",
            }
        },
    )
    sm_template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": f"/aws/vendedlogs/states/{APP_PREFIX}-feature-onboarding-dev",
            "RetentionInDays": 30,
        },
    )


def test_the_execution_cannot_poll_forever(definition):
    """120 minutes: 30 did not cover a real ingest on top of group creation."""
    assert definition["TimeoutSeconds"] == 7200


def test_ssm_parameters_published(sm_template):
    sm_template.resource_count_is("AWS::SSM::Parameter", 2)
    for name in (
        f"/{APP_PREFIX}/dev/state-machine/feature-onboarding-arn",
        f"/{APP_PREFIX}/dev/state-machine/feature-onboarding-log-group",
    ):
        sm_template.has_resource_properties("AWS::SSM::Parameter", {"Name": name})


# ── Stage 4: the transform + ingestion Processing job ───────────────────


@pytest.fixture
def create_ingest(definition):
    return definition["States"]["CreateIngestJob"]["Parameters"]


def test_the_compute_and_role_come_from_the_compiler_not_from_this_stack(
    create_ingest,
):
    """AC-4 + the compiled-intake contract: `ingest.compute` and `ingest.role_arn`
    are decided by stage 1, so the launcher passes them through rather than
    re-deriving them. A form that declares its own compute would otherwise be
    silently ignored, and this stack could disagree with the document."""
    assert (
        create_ingest["AppSpecification"]["ImageUri.$"] == "$.ingest_compute.image_uri"
    )
    cluster = create_ingest["ProcessingResources"]["ClusterConfig"]
    assert cluster["InstanceType.$"] == "$.ingest_compute.instance_type"
    assert cluster["InstanceCount.$"] == "$.ingest_compute.instance_count"
    assert create_ingest["RoleArn.$"] == "$.ingest_role_arn"
    # The volume has no intake-form field, so it stays platform-governed.
    assert cluster["VolumeSizeInGB"] == 30
    # The image bakes its own ENTRYPOINT, so the request overrides nothing.
    assert "ContainerEntrypoint" not in create_ingest["AppSpecification"]
    # No channels: the job reads the compiled intake and writes the report itself.
    assert "ProcessingInputs" not in create_ingest
    assert "ProcessingOutputConfig" not in create_ingest


def test_the_compilers_payload_survives_both_stage_two_invocations(definition):
    """`ingest_compute` and `ingest_role_arn` only reach CreateIngestJob if stage
    2's return value is collected BESIDE the state instead of replacing it."""
    states = definition["States"]
    assert states["Compile"]["OutputPath"] == "$.Payload"
    for name, path in (("CreateGroup", "$.create_group"), ("Document", "$.document")):
        assert states[name]["ResultPath"] == path
        assert states[name]["ResultSelector"] == {"payload.$": "$.Payload"}
        # Replacing the state is exactly what would drop the compiler's payload.
        assert "OutputPath" not in states[name]


def test_the_job_is_told_which_compiled_intake_to_read(definition, create_ingest):
    """The compiled intake is the contract; stage 4 reads the same object as 2 and 3."""
    resolve = definition["States"]["ResolveIngestJob"]
    # ASL cannot interpolate a path into an array element, so the argument list is
    # built with States.Array here and referenced whole by the create.
    assert resolve["Parameters"]["arguments.$"] == (
        "States.Array('--compiled-intake-s3-uri', $.compiled_intake_s3_uri, "
        "'--run-id', $.run_id)"
    )
    assert resolve["ResultPath"] == "$.ingest_job"
    assert create_ingest["AppSpecification"]["ContainerArguments.$"] == (
        "$.ingest_job.arguments"
    )


def test_the_ingest_job_name_leads_with_its_kind(definition):
    """`fi-<runid>`, matching the two M4 pipelines: no ar-<wl>-<env>- head. The kind
    has to lead, because it is all the state machine's IAM grant can be scoped by
    (`processing-job/fi-*`) — a name that stopped starting with `fi-` would be denied
    at CreateProcessingJob rather than merely look wrong."""
    resolve = definition["States"]["ResolveIngestJob"]
    assert resolve["Parameters"]["name.$"] == "States.Format('fi-{}', $.run_id)"


def test_the_job_reaches_snowflake_over_privatelink_in_the_vpc(create_ingest):
    """AC-1: PrivateLink resolves to a private address only from inside the VPC,
    and the environment carries secret IDs, never secret values."""
    assert create_ingest["NetworkConfig"]["VpcConfig"] == {
        "SecurityGroupIds": SECURITY_GROUPS,
        "Subnets": SUBNETS,
    }
    environment = create_ingest["Environment"]
    assert environment["SNOWFLAKE_ACCOUNT"].endswith(".privatelink")
    assert environment["NO_PROXY"] == "snowflakecomputing.com"
    # {env} interpolation, as the lambdas block does it.
    assert environment["ENV"] == "dev"
    for name in ("SNOWFLAKE_USER_SECRET_ID", "SNOWFLAKE_PRIVATE_KEY_SECRET_ID"):
        assert environment[name].startswith("f1ai-pwsa0002037")
    # A credential must never travel in the environment, only the ID of one.
    assert not any(
        marker in json.dumps(environment).lower()
        for marker in ("password", 'private_key"', "-----begin")
    )


def test_the_job_is_tagged_for_cost_allocation_and_the_run(create_ingest):
    """SPEC §6.2/§6.3: the tagging aspect cannot reach a job created at run time."""
    assert {"Key": "ClarityID", "Value": "96354"} in create_ingest["Tags"]
    assert {"Key": "ar:runid", "Value.$": "$.run_id"} in create_ingest["Tags"]


def test_the_ingest_job_is_polled_because_there_is_no_sync_integration(definition):
    """aws_stepfunctions_tasks has no Processing job construct, so createProcessingJob
    returns as soon as the job exists and completion has to be polled."""
    states = definition["States"]
    assert states["CreateIngestJob"]["Resource"] == (
        "arn:aws:states:::aws-sdk:sagemaker:createProcessingJob"
    )
    describe = states["DescribeIngestJob"]
    assert describe["Resource"] == (
        "arn:aws:states:::aws-sdk:sagemaker:describeProcessingJob"
    )
    assert describe["Parameters"] == {"ProcessingJobName.$": "$.ingest_job.name"}
    assert describe["ResultPath"] == "$.ingest_job.describe"

    status = "$.ingest_job.describe.ProcessingJobStatus"
    assert states["CheckIngestJobStatus"]["Choices"] == [
        {"Variable": status, "StringEquals": "Completed", "Next": "IngestComplete"},
        {"Variable": status, "StringEquals": "InProgress", "Next": "WaitForIngest"},
        {"Variable": status, "StringEquals": "Stopping", "Next": "WaitForIngest"},
    ]
    assert states["WaitForIngest"] == {
        "Type": "Wait",
        "Seconds": 30,
        "Next": "DescribeIngestJob",
    }
    assert states["IngestComplete"] == {"Type": "Succeed"}


def test_a_failed_or_stopped_ingest_fails_the_execution_with_the_describe(definition):
    """A Stopped job carries no FailureReason, and a CausePath that resolves to
    nothing would turn a clean failure into a States.Runtime error. The whole
    describe is also the best evidence available, since the notebook role cannot
    read Processing job logs."""
    states = definition["States"]
    assert states["CheckIngestJobStatus"]["Default"] == "IngestJobFailed"
    assert states["IngestJobFailed"] == {
        "Type": "Fail",
        "Error": "IngestJobFailed",
        "CausePath": "States.JsonToString($.ingest_job.describe)",
    }


def test_no_state_machines_block_deploys_nothing():
    """qa.json and prod.json are not wired; the stack must still synthesize."""
    template = _template(None)
    template.resource_count_is("AWS::StepFunctions::StateMachine", 0)
    template.resource_count_is("AWS::Logs::LogGroup", 0)
