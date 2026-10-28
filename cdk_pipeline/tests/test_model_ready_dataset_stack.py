"""Tests for CdkModelReadyDatasetStack — the AR-18-b-2 model-ready-dataset machine."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from cdk_stacks.cdk_model_ready_dataset import CdkModelReadyDatasetStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
APP_PREFIX = "novos-ar"
SUBNETS = ["subnet-0537c8dda74f4723b", "subnet-0811c3073b6ae7f26"]
SECURITY_GROUPS = ["sg-0fff0e9160460002d"]
MRD_BUCKET = f"novartis-{ACCOUNT}-dev-{REGION}-{APP_PREFIX}-mrd-datasets"


# No image, instance type, instance count, job name, role or tags here on purpose:
# all six come from the validator's return value, which reads them off the compiled
# intake. Only what the intake form has no field for is configured.
def _stage(max_runtime_seconds: int) -> dict:
    return {
        "volume_size_gb": 30,
        "max_runtime_seconds": max_runtime_seconds,
        "subnet_group": "routable",
        "security_group": "lambda",
        "env_vars": {
            "ENV": "{env}",
            "ACCOUNT_ID": "{account}",
            "AWS_REGION": "{region}",
            "KMS_KEY_ARN": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-key-id",
            "ATHENA_OUTPUT_S3_URI": (
                "s3://novartis-{account}-{env}-{region}-{app_prefix}"
                "-mrd-datasets/athena-results/"
            ),
        },
    }


STATE_MACHINE = {
    "name": "model-ready-dataset",
    "description": "NovaOS AR model-ready dataset",
    "role": "{account_role_prefix}{app_prefix}-ModelReadyDatasetStateMachine",
    "validator_lambda": "mrd-validation",
    "wait_seconds": 30,
    "timeout_minutes": 240,
    "log_retention": "ONE_MONTH",
    "pit_join": _stage(5400),
    "split": _stage(3600),
    "validate": _stage(3600),
}

SNOWFLAKE = {
    "env_vars": {
        "SNOWFLAKE_ACCOUNT": "nvs2deveast1.us-east-1.privatelink",
        "SNOWFLAKE_HOST": "nvs2deveast1.us-east-1.privatelink.snowflakecomputing.com",
        "SNOWFLAKE_WAREHOUSE": "COM_US_IMDNA_AWB_WH_S",
        "SNOWFLAKE_DATABASE": "COM_US_IMDNA_ADL",
        "SNOWFLAKE_SCHEMA": "AWB_PWSA0002037_NOVAOS",
        "SNOWFLAKE_ROLE": "F1_IMU_IM_DEV_AWB_PWSA0002037_NOVAOS_ENGINEER",
        "SNOWFLAKE_USER_SECRET_ID": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:f1ai-pwsa0002037-Gza0Cv"
        ),
        "SNOWFLAKE_PRIVATE_KEY_SECRET_ID": "f1ai-pwsa0002037-key",
        "NO_PROXY": "snowflakecomputing.com",
    }
}

# The three stages, as (jobs key, ASL state fragment, entrypoint script).
STAGES = (
    ("pit_join", "PitJoin", "pit_join.py"),
    ("split", "Split", "split.py"),
    ("validate", "ValidateAndWrite", "validate_and_write.py"),
)


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
        config[environment]["state_machines"] = {"model_ready_dataset": state_machine}
    return config


def _template(state_machine: dict = None, environment: str = "dev") -> Template:
    stack = CdkModelReadyDatasetStack(
        scope=cdk.App(),
        id="TestModelReadyDataset",
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
def create_jobs(definition):
    """Every stage's CreateProcessingJob parameters, by state fragment."""
    return {
        fragment: definition["States"][f"Create{fragment}Job"]["Parameters"]
        for _, fragment, _ in STAGES
    }


# --- the state machine itself ------------------------------------------------


def test_state_machine_created(sm_template):
    sm_template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {
            "StateMachineName": f"{APP_PREFIX}-model-ready-dataset-dev",
            "StateMachineType": "STANDARD",
        },
    )
    # Its own role, not CohortLabelStateMachine: the two pipelines invoke different
    # validators, so a shared role would grant each the other's.
    machines = sm_template.find_resources("AWS::StepFunctions::StateMachine")
    role = str(list(machines.values())[0]["Properties"]["RoleArn"])
    assert f"RRDRAR65DEV_AWS_APP01-{APP_PREFIX}-ModelReadyDatasetStateMachine" in role


def test_an_unconfigured_environment_deploys_nothing():
    """Only dev.json carries a model_ready_dataset block; qa and prod synth empty."""
    template = _template(None, environment="prod")
    assert template.find_resources("AWS::StepFunctions::StateMachine") == {}


def test_logging_is_configured_explicitly(sm_template):
    """V28: a Standard workflow logs nothing at all without this, and nothing warns
    you. Retention is set explicitly for the same reason."""
    sm_template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {"LoggingConfiguration": {"Level": "ALL", "IncludeExecutionData": True}},
    )
    sm_template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": (
                f"/aws/vendedlogs/states/{APP_PREFIX}-model-ready-dataset-dev"
            ),
            "RetentionInDays": 30,
        },
    )


# --- the definition ---------------------------------------------------------


def test_the_run_id_is_minted_as_the_first_act(definition):
    """X5 §3.1. ResultPath ADDS beside the input rather than replacing it, which is
    what lets both input shapes through without either being named here."""
    assert definition["StartAt"] == "MintRunId"
    mint = definition["States"]["MintRunId"]
    assert mint["Type"] == "Pass"
    assert mint["ResultPath"] == "$.platform"
    assert set(mint["Parameters"]) == {"generated_run_id.$"}
    assert mint["Next"] == "Validate"


def test_the_validator_receives_the_whole_execution_input(definition):
    validate = definition["States"]["Validate"]
    assert (
        validate["Parameters"]["FunctionName"]
        == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{APP_PREFIX}-mrd-validation-dev"
    )
    assert validate["Parameters"]["Payload.$"] == "$"
    # Its return value becomes the state: it carries the `compute` and `jobs` blocks
    # all three stages read.
    assert validate["OutputPath"] == "$.Payload"
    assert validate["Next"] == "ResolvePitJoinJob"


def test_the_definition_needs_no_branch_per_input_shape(definition):
    """An MRD form is located by model alone, or by staged directory URI. Deciding
    which in ASL would cost a Choice per optional field; the handler does it."""
    body = json.dumps(definition)
    assert "$.model_id" not in body
    assert "$.model_ready_dataset_intake_s3_uri" not in body


def test_the_three_stages_run_in_order(definition):
    """AC-4/AC-5: the join feeds the split, and the split is strictly before the
    validation gate. A Completed job goes to the NEXT stage, never straight to
    Succeed."""
    states = definition["States"]
    created = [
        name
        for name, state in states.items()
        if state.get("Resource", "").endswith("createProcessingJob")
    ]
    assert created == [
        "CreatePitJoinJob",
        "CreateSplitJob",
        "CreateValidateAndWriteJob",
    ]

    def completed_goes_to(fragment):
        choices = states[f"Check{fragment}JobStatus"]["Choices"]
        return {c["StringEquals"]: c["Next"] for c in choices}["Completed"]

    assert completed_goes_to("PitJoin") == "ResolveSplitJob"
    assert completed_goes_to("Split") == "ResolveValidateAndWriteJob"
    # Only the last stage succeeds the execution.
    assert completed_goes_to("ValidateAndWrite") == "ModelReadyDatasetComplete"


def test_every_stage_sets_its_own_container_entrypoint(create_jobs):
    """One image serves all three stages and its baked ENTRYPOINT is pit_join.py, so
    the default would silently run stage 2's script three times. Required on the PIT
    join too, not just the other two."""
    for _, fragment, script in STAGES:
        entrypoint = create_jobs[fragment]["AppSpecification"]["ContainerEntrypoint"]
        assert entrypoint == [
            "python3",
            f"/novos/src/processing/model-ready-dataset/{script}",
        ]
    # Three distinct scripts — not the same one three times.
    scripts = {
        create_jobs[f]["AppSpecification"]["ContainerEntrypoint"][1]
        for _, f, _ in STAGES
    }
    assert len(scripts) == 3


def test_each_stage_reads_its_own_block_of_the_compiled_intake(create_jobs, definition):
    """V26: the 63-character cap is asserted by the compiler, so reusing its value
    inherits the assertion instead of composing a second name that could differ.
    Each stage must read ITS OWN jobs.<stage> block — crossing them would run one
    stage under another's name and role."""
    for key, fragment, _ in STAGES:
        create = create_jobs[fragment]
        assert create["ProcessingJobName.$"] == f"$.jobs.{key}.processing_job_name"
        # AC-2: CohortLabelExecution, never the broad dev execution role.
        assert create["RoleArn.$"] == f"$.jobs.{key}.role_arn"
        # Describe must agree with Create, or the wait loop polls another job.
        describe = definition["States"][f"Describe{fragment}Job"]["Parameters"]
        assert describe["ProcessingJobName.$"] == f"$.jobs.{key}.processing_job_name"
    # Nothing in the definition builds a job name.
    assert "ar-{}-dev" not in json.dumps(definition)


def test_the_tags_come_from_the_compiled_intake(create_jobs):
    """SPEC-M4 §6.2 wants the six mandatory tags plus ar:runid AND ar:owner.
    `mrd_compiler.run_tags` already resolved all eight, so they are read rather than
    rebuilt from the `tags` block — which carries no ar:owner to rebuild from."""
    for key, fragment, _ in STAGES:
        assert create_jobs[fragment]["Tags.$"] == f"$.jobs.{key}.tags"


def test_the_compute_comes_from_the_compiled_intake(create_jobs):
    """A form that declares its own compute is honoured, and the state machine cannot
    disagree with the document. All three stages share the one image."""
    for _, fragment, _ in STAGES:
        create = create_jobs[fragment]
        assert create["AppSpecification"]["ImageUri.$"] == "$.compute.image_uri"
        cluster = create["ProcessingResources"]["ClusterConfig"]
        assert cluster["InstanceType.$"] == "$.compute.instance_type"
        assert cluster["InstanceCount.$"] == "$.compute.instance_count"
        # Not in the form, so not in the document: the volume is platform-governed.
        assert cluster["VolumeSizeInGB"] == 30


def test_the_container_arguments_are_the_jobs_contract(definition):
    """All three stages take the same two arguments. Every output location is fixed at
    compile time, so nothing else has to be passed between states."""
    for _, fragment, _ in STAGES:
        arguments = definition["States"][f"Resolve{fragment}Job"]["Parameters"][
            "arguments.$"
        ]
        assert arguments == (
            "States.Array('--compiled-intake-s3-uri', $.compiled_intake_s3_uri, "
            "'--run-id', $.run_id)"
        )


def test_no_stage_takes_a_processing_channel(create_jobs):
    """boto3 everywhere, so every S3 call carries ExpectedBucketOwner — which a
    ProcessingInput download cannot. Adding a channel would also need
    ProcessingOutputConfig.KmsKeyId, since the bucket denies non-KMS puts."""
    for _, fragment, _ in STAGES:
        assert "ProcessingInputs" not in create_jobs[fragment]
        assert "ProcessingOutputConfig" not in create_jobs[fragment]


def test_every_stage_runs_in_the_vpc(create_jobs):
    """The offline store's Athena path and a snowflake-sourced cohort's PrivateLink
    hostname both resolve to private addresses only from inside the VPC."""
    for _, fragment, _ in STAGES:
        vpc = create_jobs[fragment]["NetworkConfig"]["VpcConfig"]
        assert vpc["Subnets"] == SUBNETS
        assert vpc["SecurityGroupIds"] == SECURITY_GROUPS


# --- the jobs' environment --------------------------------------------------


def test_the_environment_carries_every_variable_the_jobs_require(create_jobs):
    """The jobs' config.py refuses to start without these, so a missing one here is a
    deploy-time break the job can only report at run time. ATHENA_OUTPUT_S3_URI is in
    its REQUIRED tuple, so all three stages need it even though only the PIT join
    queries Athena."""
    required = (
        "ENV",
        "ACCOUNT_ID",
        "AWS_REGION",
        "KMS_KEY_ARN",
        "ATHENA_OUTPUT_S3_URI",
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_HOST",
        "SNOWFLAKE_USER_SECRET_ID",
        "SNOWFLAKE_PRIVATE_KEY_SECRET_ID",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_SCHEMA",
        "SNOWFLAKE_ROLE",
    )
    for _, fragment, _ in STAGES:
        environment = create_jobs[fragment]["Environment"]
        assert not [name for name in required if name not in environment]


def test_placeholders_are_interpolated(create_jobs):
    """{account} and {region} as well as {env} — so no account id or region has to be
    written into the config file as a literal (SPEC-M4 §6.1)."""
    environment = create_jobs["PitJoin"]["Environment"]
    assert environment["ENV"] == "dev"
    assert environment["ACCOUNT_ID"] == ACCOUNT
    assert environment["AWS_REGION"] == REGION
    # The Athena results location interpolates four placeholders at once.
    assert environment["ATHENA_OUTPUT_S3_URI"] == f"s3://{MRD_BUCKET}/athena-results/"
    # No placeholder left unresolved anywhere — a literal "{env}" would reach the
    # container as-is and fail only at run time.
    assert [name for name, value in environment.items() if "{" in value] == []


def test_the_secret_variables_carry_arns_never_values(create_jobs):
    """SPEC-M4 §6.5. Nothing in the jobs' environment is a credential."""
    for _, fragment, _ in STAGES:
        environment = create_jobs[fragment]["Environment"]
        assert environment["SNOWFLAKE_USER_SECRET_ID"].startswith(
            "arn:aws:secretsmanager:"
        )
        assert "SNOWFLAKE_PASSWORD" not in environment
        # A PEM would contain this; a secret id cannot.
        assert "BEGIN" not in json.dumps(environment)


# --- the wait loops ---------------------------------------------------------


def test_every_stage_has_its_own_wait_loop(definition):
    """Three independent loops. A stage sharing another's Describe state would poll
    the wrong job name and either hang or succeed on someone else's result."""
    states = definition["States"]
    for _, fragment, _ in STAGES:
        assert states[f"Wait{'For'}{fragment}"]["Next"] == f"Describe{fragment}Job"
        assert states[f"Describe{fragment}Job"]["Next"] == f"Check{fragment}JobStatus"
        choice = states[f"Check{fragment}JobStatus"]
        routes = {c["StringEquals"]: c["Next"] for c in choice["Choices"]}
        assert routes["InProgress"] == f"WaitFor{fragment}"
        assert routes["Stopping"] == f"WaitFor{fragment}"
        # Failed, Stopped, and any status a job gains later.
        assert choice["Default"] == f"{fragment}JobFailed"
