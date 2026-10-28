"""CdkCohortLabelStack — the NOVOS AR cohort-and-label state machine (AR-18-a-2).

The Standard workflow that mints a run id, compiles the cohort-label intake form,
and then runs the population + label Processing job:

    MintRunId -> Validate (stage 1, the cohort-validation Lambda)
      -> ResolvePopulationLabelJob -> CreatePopulationLabelJob (stage 2)
      -> DescribePopulationLabelJob -> CheckPopulationLabelJobStatus
             Completed             -> PopulationLabelComplete
             InProgress / Stopping -> WaitForPopulationLabel -> Describe...
             anything else         -> PopulationLabelJobFailed

**Two stages, not three.** AC-1/AC-3/AC-4/AC-5 describe three Processing jobs —
population, label and cohort assembly — and have not been reworded on Confluence.
Population and label consume the SAME Snowflake read, so splitting them pays the
connection and query cost twice and lands an intermediate nothing reads (SPEC-M4
§2.1, §3.3); cohort assembly moved to the model-ready-dataset pipeline (NTU-1038).

The wait loop is a native `aws-sdk` integration rather than a Lambda: it runs in the
Step Functions service instead of the VPC, so it needs no SageMaker VPC endpoint and
no extra image. It has to be a loop because `aws_stepfunctions_tasks` carries no
Processing job construct at all, so there is no `.sync` integration and
`createProcessingJob` returns as soon as the job is created — the same constraint the
feature-onboarding state machine works around.

AC-3/AC-4's "an unrecognized value is rejected before any AWS call" is met by
`Validate`, not here: the Lambda imports every module the form names and checks its
params against the real signature before the Processing job is ever submitted.

The job needs no input or output channels. `src/common`, the entrypoint and any
form-local `local_transformations` tree are baked into its image, and it reads the
compiled intake and writes its own outputs so that every S3 call carries
`ExpectedBucketOwner` — which a `ProcessingInput` download cannot.

Execution input — `run_id` is always optional, and the intake is named EITHER by
model, OR by the staged directory's S3 URI. A cohort-label form is located by MODEL
alone; `feature_group_name` is carried forward for linkage only, never as a locator:

    {
      "model_id":   "cosentyx/patient_outcome_mortality",
      "run_id":     "r5f3a9c2d1e0b",              # optional
      "as_of":      "2026-09-15T00:00:00Z"        # optional, defaults to now
    }

    {
      "cohort_label_intake_s3_uri":
          "s3://<config bucket>/cohort-label-intake/<brand>/<model>/",
      "run_id":     "r5f3a9c2d1e0b"               # optional
    }

Config in environments/<env>.json under "state_machines":

    "cohort_label": {
      "name": "cohort-label",
      "description": "...",
      "role": "{account_role_prefix}{app_prefix}-CohortLabelStateMachine",
      "validator_lambda": "cohort-validation",
      "wait_seconds": 30,
      "timeout_minutes": 120,
      "log_retention": "ONE_MONTH",
      "population_label": {
        "volume_size_gb": 30,
        "max_runtime_seconds": 5400,
        "subnet_group": "routable",
        "security_group": "lambda",
        "env_vars": { "ENV": "{env}", ... }
      }
    }

`name` is the short form: the full state machine name is composed here as
`{app_prefix}-{name}-{environment}`, as cdk_lambdas.py composes function names.

The job's name, image, instance type, instance count and role are deliberately NOT
config: they come from the validator's return value, which reads them off the
compiled intake. That is what keeps the state machine from disagreeing with the
document — including the 63-character job-name assertion (V26), which stage 1 has
already made. Only what the intake form has no field for — the volume, the runtime
cap, the subnets and the security group — is configured here.

`population_label.env_vars` is merged with the top-level `snowflake.env_vars` block.
Snowflake is separate because those settings are not specific to this state machine,
and because no Snowflake detail is allowed in the compiled intake (SPEC-M4 §6.5);
those variables carry secret ARNs, never secret values.
"""

from typing import Any

from aws_cdk import Duration, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from cdk_common import add_state_machine
from constructs import Construct

# ASL has no substring intrinsic, and States.UUID() splits on "-" into groups of 8,
# 4, 4, 4 and 12 characters — no combination sums to 11, so the 12-character group is
# taken whole. That is why the run id is 13 characters (r + 12) rather than the 12
# SPEC-M4 §6.3 describes; hex is a subset of the base32 charset the Lambda accepts,
# and `intake_schema.is_valid_run_id` is the single definition both stages share.
RUN_ID_EXPRESSION = (
    "States.Format('r{}', "
    "States.ArrayGetItem(States.StringSplit(States.UUID(), '-'), 4))"
)

COHORT_LABEL_KEY = "cohort_label"

# Where the state machine puts what it minted, beside the caller's own input.
PLATFORM_PATH = "$.platform"
# LambdaInvoke wraps a handler's return value in Payload.
LAMBDA_PAYLOAD_PATH = "$.Payload"

# The validator's return value IS the state after Validate, because everything the
# later states read is in it.
RUN_ID_PATH = "$.run_id"
COMPILED_INTAKE_PATH = "$.compiled_intake_s3_uri"
# Composed and length-asserted by the compiler (V26), not rebuilt here.
PROCESSING_JOB_NAME_PATH = "$.processing_job_name"
# The form's chosen compute, and the role AC-2 requires the job to run as.
COMPUTE_PATH = "$.compute"
ROLE_ARN_PATH = "$.role_arn"

# Stage 2's own state, all under one key so the describe response sits beside the job
# name it describes.
JOB_PATH = "$.population_label_job"
JOB_ARGUMENTS_PATH = f"{JOB_PATH}.arguments"
JOB_CREATED_PATH = f"{JOB_PATH}.created"
JOB_DESCRIBE_PATH = f"{JOB_PATH}.describe"
JOB_STATUS_PATH = f"{JOB_DESCRIBE_PATH}.ProcessingJobStatus"


def _interpolate(env_vars, replacements):
    """`{app_prefix}` / `{env}` / `{account}` / `{region}` interpolation.

    Wider than the `lambdas` block's two placeholders so the account id and region
    stay out of the config file as literals (SPEC-M4 §6.1).
    """
    resolved = {}
    for key, value in env_vars.items():
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
        resolved[key] = value
    return resolved


class CdkCohortLabelStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        environment: str,
        config: dict[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        var_config = config[environment]
        app_prefix = var_config["app_prefix"]
        account = var_config["account"]
        region = var_config["region"]
        account_role_prefix = var_config.get("account_role_prefix", "")

        sm_config = var_config.get("state_machines", {}).get(COHORT_LABEL_KEY)
        if not sm_config:
            # Only dev.json is wired; qa and prod deploy nothing here.
            return

        name = sm_config["name"]
        state_machine_name = f"{app_prefix}-{name}-{environment}"

        # ── Stage 1: the intake compiler, named as cdk_lambdas.py names it ──
        validator = lambda_.Function.from_function_arn(
            self,
            "CohortValidationFunction",
            f"arn:aws:lambda:{region}:{account}:function:"
            f"{app_prefix}-{sm_config['validator_lambda']}-{environment}",
        )

        mint_run_id = sfn.Pass(
            self,
            "MintRunId",
            parameters={
                "generated_run_id": sfn.JsonPath.string_at(RUN_ID_EXPRESSION),
            },
            result_path=PLATFORM_PATH,
        )

        # The validator's return value replaces the state: it echoes its own inputs
        # forward alongside what stage 2 needs, so nothing earlier has to be kept.
        validate = tasks.LambdaInvoke(
            self,
            "Validate",
            lambda_function=validator,
            # The whole execution input, so stage 1 resolves the shape itself —
            # by model, or by staged directory URI.
            payload=sfn.TaskInput.from_json_path_at("$"),
            output_path=LAMBDA_PAYLOAD_PATH,
        )

        # ── Stage 2: the population + label Processing job ────────────────
        job_config = sm_config["population_label"]
        network = var_config["network"]
        processing_job_arn = f"arn:aws:sagemaker:{region}:{account}:processing-job/cl-*"

        replacements = {
            "{app_prefix}": app_prefix,
            "{env}": environment,
            "{account}": account,
            "{region}": region,
        }
        # Nothing here is a credential: the two Snowflake secret variables carry ARNs
        # the job resolves itself (SPEC-M4 §6.5).
        job_environment = {
            **_interpolate(job_config["env_vars"], replacements),
            **_interpolate(var_config["snowflake"]["env_vars"], replacements),
        }

        # SPEC-M4 §6.2: the six mandatory cost-allocation tags, which the app-wide
        # CdkTaggingAspect cannot reach on a job created at run time, plus ar:runid.
        job_tags = [
            {"Key": key, "Value": value}
            for key, value in _interpolate(var_config["tags"], replacements).items()
        ]
        job_tags.append(
            {"Key": "ar:runid", "Value": sfn.JsonPath.string_at(RUN_ID_PATH)}
        )

        # The container arguments are built once, here, so Create and Describe agree.
        # The job NAME is not built: it comes from the compiled intake.
        resolve_job = sfn.Pass(
            self,
            "ResolvePopulationLabelJob",
            parameters={
                "arguments": sfn.JsonPath.array(
                    "--compiled-intake-s3-uri",
                    sfn.JsonPath.string_at(COMPILED_INTAKE_PATH),
                    "--run-id",
                    sfn.JsonPath.string_at(RUN_ID_PATH),
                ),
            },
            result_path=JOB_PATH,
        )

        create_job = tasks.CallAwsService(
            self,
            "CreatePopulationLabelJob",
            service="sagemaker",
            action="createProcessingJob",
            iam_action="sagemaker:CreateProcessingJob",
            iam_resources=[processing_job_arn],
            parameters={
                # Composed and length-asserted by stage 1 (V26).
                "ProcessingJobName": sfn.JsonPath.string_at(PROCESSING_JOB_NAME_PATH),
                "AppSpecification": {
                    "ImageUri": sfn.JsonPath.string_at(f"{COMPUTE_PATH}.image_uri"),
                    # No ContainerEntrypoint: the image bakes one in
                    "ContainerArguments": sfn.JsonPath.list_at(JOB_ARGUMENTS_PATH),
                },
                "ProcessingResources": {
                    "ClusterConfig": {
                        "InstanceType": sfn.JsonPath.string_at(
                            f"{COMPUTE_PATH}.instance_type"
                        ),
                        "InstanceCount": sfn.JsonPath.number_at(
                            f"{COMPUTE_PATH}.instance_count"
                        ),
                        # Not in the form, so not in the document: the volume is
                        # platform-governed.
                        "VolumeSizeInGB": job_config["volume_size_gb"],
                    }
                },
                # Gold is reached over PrivateLink, which resolves to a private
                # address only from inside the VPC.
                "NetworkConfig": {
                    "VpcConfig": {
                        "SecurityGroupIds": network["security_groups"][
                            job_config["security_group"]
                        ],
                        "Subnets": network["subnet_groups"][job_config["subnet_group"]],
                    }
                },
                # AC-2: CohortLabelExecution, taken from the document rather than
                # resolved here, which is why the compiler puts it there.
                "RoleArn": sfn.JsonPath.string_at(ROLE_ARN_PATH),
                "Environment": job_environment,
                "StoppingCondition": {
                    "MaxRuntimeInSeconds": job_config["max_runtime_seconds"]
                },
                "Tags": job_tags,
            },
            result_path=JOB_CREATED_PATH,
        )

        describe_job = tasks.CallAwsService(
            self,
            "DescribePopulationLabelJob",
            service="sagemaker",
            action="describeProcessingJob",
            iam_action="sagemaker:DescribeProcessingJob",
            iam_resources=[processing_job_arn],
            parameters={
                "ProcessingJobName": sfn.JsonPath.string_at(PROCESSING_JOB_NAME_PATH)
            },
            result_path=JOB_DESCRIBE_PATH,
        )

        wait_for_job = sfn.Wait(
            self,
            "WaitForPopulationLabel",
            time=sfn.WaitTime.duration(Duration.seconds(sm_config["wait_seconds"])),
        )

        job_complete = sfn.Succeed(self, "PopulationLabelComplete")

        job_failed = sfn.Fail(
            self,
            "PopulationLabelJobFailed",
            error="PopulationLabelJobFailed",
            # The whole describe response, which carries SageMaker's FailureReason.
            cause_path=f"States.JsonToString({JOB_DESCRIBE_PATH})",
        )

        check_status = (
            sfn.Choice(self, "CheckPopulationLabelJobStatus")
            .when(
                sfn.Condition.string_equals(JOB_STATUS_PATH, "Completed"),
                job_complete,
            )
            .when(
                sfn.Condition.string_equals(JOB_STATUS_PATH, "InProgress"),
                wait_for_job,
            )
            .when(
                sfn.Condition.string_equals(JOB_STATUS_PATH, "Stopping"),
                wait_for_job,
            )
            # Failed, Stopped, and any status a job gains later.
            .otherwise(job_failed)
        )

        wait_for_job.next(describe_job)

        definition = mint_run_id
        mint_run_id.next(validate).next(resolve_job).next(create_job).next(
            describe_job
        ).next(check_status)

        # ── The state machine (shared tail; logical IDs preserved) ────────
        add_state_machine(
            self,
            id_prefix="StateMachine",
            state_machine_name=state_machine_name,
            definition=definition,
            role_config=sm_config["role"],
            account_role_prefix=account_role_prefix,
            app_prefix=app_prefix,
            environment=environment,
            ssm_prefix=name,
            timeout=Duration.minutes(sm_config["timeout_minutes"]),
            comment=sm_config["description"],
            log_retention=sm_config["log_retention"],
        )
