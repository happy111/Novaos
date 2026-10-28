"""Tests for CdkCohortLabelStack — the AR-18-a-2 cohort-and-label state machine."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from cdk_stacks.cdk_cohort_label import CdkCohortLabelStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
APP_PREFIX = "novos-ar"
SUBNETS = ["subnet-0537c8dda74f4723b", "subnet-0811c3073b6ae7f26"]
SECURITY_GROUPS = ["sg-0fff0e9160460002d"]

# No image, instance type, instance count, job name or role here on purpose: all
# five come from the validator's return value, which reads them off the compiled
# intake. Only what the intake form has no field for is configured.
POPULATION_LABEL = {
    "volume_size_gb": 30,
    "max_runtime_seconds": 5400,
    "subnet_group": "routable",
    "security_group": "lambda",
    "env_vars": {
        "ENV": "{env}",
        "ACCOUNT_ID": "{account}",
        "AWS_REGION": "{region}",
        "KMS_KEY_ARN": "arn:aws:kms:us-east-1:123456789012:key/test-key-id",
    },
}

STATE_MACHINE = {
    "name": "cohort-label",
    "description": "NovaOS AR cohort and label engineering",
    "role": "{account_role_prefix}{app_prefix}-CohortLabelStateMachine",
    "validator_lambda": "cohort-validation",
    "wait_seconds": 30,
    "timeout_minutes": 120,
    "log_retention": "ONE_MONTH",
    "population_label": POPULATION_LABEL,
}

SNOWFLAKE = {
    "env_vars": {
        "SNOWFLAKE_ACCOUNT": "nvs2deveast1.us-east-1.privatelink",
        "SNOWFLAKE_HOST": "nvs2deveast1.us-east-1.privatelink.snowflakecomputing.com",
        "SNOWFLAKE_WAREHOUSE": "COM_US_IMDNA_AWB_WH_S",
        "SNOWFLAKE_WAREHOUSE_PATTERN": "COM_US_IMDNA_AWB_WH_{size}",
        "SNOWFLAKE_DATABASE": "COM_US_IMDNA_ADL",
        "SNOWFLAKE_SCHEMA": "AWB_PWSA0002037_NOVAOS",
        "SNOWFLAKE_ROLE": "F1_IMU_IM_DEV_AWB_PWSA0002037_NOVAOS_ENGINEER",
        "SNOWFLAKE_USER_SECRET_ID": "arn:aws:secretsmanager:us-east-1:123456789012:secret:f1ai-pwsa0002037-Gza0Cv",
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
        config[environment]["state_machines"] = {"cohort_label": state_machine}
    return config


def _template(state_machine: dict = None, environment: str = "dev") -> Template:
    stack = CdkCohortLabelStack(
        scope=cdk.App(),
        id="TestCohortLabel",
        environment=environment,
        config=_state_machine_config(environment, state_machine),
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


@pytest.fixture
def create_job(definition):
    return definition["States"]["CreatePopulationLabelJob"]["Parameters"]


# --- the state machine itself ------------------------------------------------


def test_state_machine_created(sm_template):
    sm_template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {
            "StateMachineName": f"{APP_PREFIX}-cohort-label-dev",
            "StateMachineType": "STANDARD",
        },
    )
    # The role is created by CdkIamDeployRolesStack and imported by name here.
    machines = sm_template.find_resources("AWS::StepFunctions::StateMachine")
    role = str(list(machines.values())[0]["Properties"]["RoleArn"])
    assert f"RRDRAR65DEV_AWS_APP01-{APP_PREFIX}-CohortLabelStateMachine" in role


def test_an_unconfigured_environment_deploys_nothing(sm_template):
    """Only dev.json carries a cohort_label block; qa and prod synthesize empty."""
    template = _template(None, environment="prod")
    assert template.find_resources("AWS::StepFunctions::StateMachine") == {}


def test_logging_is_configured_explicitly(sm_template):
    """AC-6 / V28: a Standard workflow logs nothing at all without this, and nothing
    warns you. Retention is set explicitly for the same reason."""
    sm_template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {"LoggingConfiguration": {"Level": "ALL", "IncludeExecutionData": True}},
    )
    sm_template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": f"/aws/vendedlogs/states/{APP_PREFIX}-cohort-label-dev",
            "RetentionInDays": 30,
        },
    )


# --- the definition ---------------------------------------------------------


def test_two_stages_not_three(definition):
    """AC-1/AC-3/AC-4/AC-5 read as three Processing jobs and have not been reworded.
    Population and label share one Snowflake read, so they are one job; cohort
    assembly moved to the MRD pipeline (SPEC-M4 §3.3)."""
    states = definition["States"]
    created = [
        name
        for name, state in states.items()
        if state.get("Resource", "").endswith("createProcessingJob")
    ]
    assert created == ["CreatePopulationLabelJob"]


def test_the_run_id_is_minted_as_the_first_act(definition):
    """X5 §3.1. ResultPath ADDS beside the input rather than replacing it, which is
    what lets both input shapes through without either being named here."""
    assert definition["StartAt"] == "MintRunId"
    mint = definition["States"]["MintRunId"]
    assert mint["Type"] == "Pass"
    assert mint["ResultPath"] == "$.platform"
    assert set(mint["Parameters"]) == {"generated_run_id.$"}
    assert mint["Next"] == "Validate"


def test_the_definition_needs_no_branch_per_input_shape(definition):
    """A cohort-label form is located by model alone, or by staged directory URI.
    Deciding which in ASL would cost a Choice per optional field; the handler does it.
    """
    body = json.dumps(definition)
    assert "$.model_id" not in body
    assert "$.cohort_label_intake_s3_uri" not in body
    # feature_group_name is linkage only, never a locator (handoff §6.1).
    assert "feature_group_name" not in body


def test_the_validator_receives_the_whole_execution_input(definition):
    validate = definition["States"]["Validate"]
    assert (
        validate["Parameters"]["FunctionName"]
        == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{APP_PREFIX}-cohort-validation-dev"
    )
    assert validate["Parameters"]["Payload.$"] == "$"
    # Its return value becomes the state, because everything stage 2 reads is in it.
    assert validate["OutputPath"] == "$.Payload"
    assert validate["Next"] == "ResolvePopulationLabelJob"


def test_the_states_run_in_order(definition):
    states = definition["States"]
    assert states["ResolvePopulationLabelJob"]["Next"] == "CreatePopulationLabelJob"
    assert states["CreatePopulationLabelJob"]["Next"] == "DescribePopulationLabelJob"
    assert (
        states["DescribePopulationLabelJob"]["Next"] == "CheckPopulationLabelJobStatus"
    )


# --- what the job is created with -------------------------------------------


def test_the_job_name_comes_from_the_compiled_intake(definition, create_job):
    """V26: the 63-character cap is asserted by the compiler, so reusing its value
    inherits the assertion instead of composing a second name that could differ."""
    assert create_job["ProcessingJobName.$"] == "$.processing_job_name"
    # Nothing in the definition builds a job name.
    assert "ar-{}-dev" not in json.dumps(definition)
    assert (
        definition["States"]["DescribePopulationLabelJob"]["Parameters"][
            "ProcessingJobName.$"
        ]
        == "$.processing_job_name"
    )


def test_the_compute_comes_from_the_compiled_intake(create_job):
    """A form that declares its own compute is honoured, and the state machine
    cannot disagree with the document."""
    assert create_job["AppSpecification"]["ImageUri.$"] == "$.compute.image_uri"
    cluster = create_job["ProcessingResources"]["ClusterConfig"]
    assert cluster["InstanceType.$"] == "$.compute.instance_type"
    assert cluster["InstanceCount.$"] == "$.compute.instance_count"
    # Not in the form, so not in the document: the volume is platform-governed.
    assert cluster["VolumeSizeInGB"] == 30


def test_the_job_runs_as_cohort_label_execution(create_job):
    """AC-2: never the broad dev execution role. Taken from the document, which is
    why the compiler puts it there."""
    assert create_job["RoleArn.$"] == "$.role_arn"


def test_no_container_entrypoint_is_sent(create_job):
    """Processing job arguments REPLACE the image's CMD. The image bakes an
    ENTRYPOINT, so there is one definition of how the job starts."""
    assert "ContainerEntrypoint" not in create_job["AppSpecification"]
    assert (
        create_job["AppSpecification"]["ContainerArguments.$"]
        == "$.population_label_job.arguments"
    )


def test_the_container_arguments_are_the_jobs_contract(definition):
    arguments = definition["States"]["ResolvePopulationLabelJob"]["Parameters"][
        "arguments.$"
    ]
    assert arguments == (
        "States.Array('--compiled-intake-s3-uri', $.compiled_intake_s3_uri, "
        "'--run-id', $.run_id)"
    )


def test_the_job_runs_in_the_vpc(create_job):
    """Gold is reached over PrivateLink, which resolves to a private address only
    from inside the VPC."""
    vpc = create_job["NetworkConfig"]["VpcConfig"]
    assert vpc["Subnets"] == SUBNETS
    assert vpc["SecurityGroupIds"] == SECURITY_GROUPS


def test_the_job_carries_the_mandatory_tags_plus_the_run_id(create_job):
    """SPEC-M4 §6.2: CdkTaggingAspect cannot reach a job created at run time."""
    tags = create_job["Tags"]
    keys = [tag["Key"] for tag in tags]
    assert "ClarityID" in keys and "Environment" in keys
    run_id_tag = [tag for tag in tags if tag["Key"] == "ar:runid"]
    assert run_id_tag == [{"Key": "ar:runid", "Value.$": "$.run_id"}]


# --- the job's environment --------------------------------------------------
def test_the_secret_variables_carry_arns_never_values(create_job):
    """SPEC-M4 §6.5. Nothing in the job's environment is a credential."""
    environment = create_job["Environment"]
    assert environment["SNOWFLAKE_USER_SECRET_ID"].startswith("arn:aws:secretsmanager:")
    assert "SNOWFLAKE_PASSWORD" not in environment
    assert "PRIVATE_KEY" not in json.dumps(
        {k: v for k, v in environment.items() if not k.endswith("_SECRET_ID")}
    )
    # A PEM would contain this; a secret id cannot.
    assert "BEGIN" not in json.dumps(environment)


def test_placeholders_are_interpolated(create_job):
    """{account} and {region} as well as {env} — so no account id or region has to
    be written into the config file as a literal (SPEC-M4 §6.1)."""
    environment = create_job["Environment"]
    assert environment["ENV"] == "dev"
    assert environment["ACCOUNT_ID"] == ACCOUNT
    assert environment["AWS_REGION"] == REGION
    assert "{" not in environment["ACCOUNT_ID"]


def test_the_environment_carries_every_variable_the_job_requires(create_job):
    """The job's config.py refuses to start without these, so a missing one here is
    a deploy-time break the job can only report at run time."""
    environment = create_job["Environment"]
    required = (
        "ENV",
        "ACCOUNT_ID",
        "AWS_REGION",
        "KMS_KEY_ARN",
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_HOST",
        "SNOWFLAKE_USER_SECRET_ID",
        "SNOWFLAKE_PRIVATE_KEY_SECRET_ID",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_SCHEMA",
        "SNOWFLAKE_ROLE",
    )
    assert not [name for name in required if name not in environment]


# --- the wait loop ----------------------------------------------------------
def test_the_status_choice_covers_every_terminal_state(definition):
    choice = definition["States"]["CheckPopulationLabelJobStatus"]
    routes = {c["StringEquals"]: c["Next"] for c in choice["Choices"]}
    assert routes["Completed"] == "PopulationLabelComplete"
    assert routes["InProgress"] == "WaitForPopulationLabel"
    assert routes["Stopping"] == "WaitForPopulationLabel"
    # Failed, Stopped, and any status a job gains later.
    assert choice["Default"] == "PopulationLabelJobFailed"
