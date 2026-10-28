"""CdkFeatureOnboardingStack — the NOVOS AR feature onboarding state machine.

Stages 3 and 4 of the feature-ingestion pipeline: the Standard workflow that
chains the two Lambda stages, waits for the feature group to reach `Created`, and
then runs the transform + ingestion Processing job.

    MintRunId -> Compile (stage 1)
      -> CreateGroup (stage 2, action=create)
      -> DescribeFeatureGroup -> CheckFeatureGroupStatus
             Created  -> Document (stage 2, action=document)
             Creating -> WaitForCreated -> DescribeFeatureGroup
             anything else -> FeatureGroupCreateFailed
      -> ResolveIngestJob -> CreateIngestJob (stage 4)
      -> DescribeIngestJob -> CheckIngestJobStatus
             Completed             -> IngestComplete
             InProgress / Stopping -> WaitForIngest -> DescribeIngestJob
             anything else         -> IngestJobFailed

Both wait loops are native `aws-sdk` integrations rather than Lambdas: they run
in the Step Functions service instead of the VPC, so they need no SageMaker VPC
endpoint and no extra container image. Stage 4 has to be one because
`aws_stepfunctions_tasks` has no Processing job construct at all (checked against
aws-cdk-lib 2.269.0) — `SageMakerCreateTrainingJob` and
`SageMakerCreateTransformJob` exist, processing does not — so there is no `.sync`
integration and `createProcessingJob` returns as soon as the job is created.

`Document` is not optional. `UpdateFeatureMetadata` only applies to a group that
has reached `Created`, and `CreateFeatureGroup` cannot carry a feature
description, so without this second invocation no feature description ever
reaches the store (AR-05-d AC-4).

The ingestion job needs no input or output channels: `src/common` and the
entrypoint are baked into its image, and it reads the compiled intake and writes
the ingest report itself so that every S3 call carries `ExpectedBucketOwner`.

Execution input — `run_id` is always optional, and the feature intake is named
EITHER by model and feature group, OR by the directory's S3 URI:

    {
      "model_id":            "brand_abc/patient_outcome_mortality",
      "feature_group_name":  "demo_brandabc_cardiology_patient_outcome_metrics",
      "run_id":              "rdev0915liv2"          # optional
    }

    {
      "feature_intake_s3_uri":
          "s3://<config bucket>/feature-intake/<model_id>/<feature group>/",
      "run_id":              "rdev0915liv2"          # optional
    }

Config in environments/<env>.json under "state_machines":

    "state_machines": {
      "feature_onboarding": {
        "name": "feature-onboarding",
        "description": "...",
        "role": "{account_role_prefix}{app_prefix}-statemachine-feature-onboarding",
        "compiler_lambda": "feature-intake-compiler",
        "creater_lambda": "feature-group-creater",
        "wait_seconds": 30,
        "timeout_minutes": 120,
        "log_retention": "ONE_MONTH",
        "ingest": {
          "volume_size_gb": 30,
          "max_runtime_seconds": 5400,
          "subnet_group": "routable",
          "security_group": "lambda",
          "env_vars": { "ENV": "{env}", ... }
        }
      }
    }

`name` is the short form: the full state machine name is composed here as
`{app_prefix}-{name}-{environment}`, as cdk_lambdas.py composes function names.

The ingest job's image, instance type, instance count and role are deliberately
NOT config: they come from `ingest_compute` and `ingest_role_arn` in stage 1's
return value, so a form that declares its own compute is honoured and the state
machine cannot disagree with the compiled intake. Only what the intake form has no
field for — the volume, the runtime cap, the subnets and the security group — is
configured here.

`compiler_lambda` and `creater_lambda` are keys of the `lambdas` block, whose
function names cdk_lambdas.py composes the same way. `log_retention` is an
aws_logs.RetentionDays member name. `subnet_group` and `security_group` are keys
of the `network` block. The ingestion job's environment is `ingest.env_vars`
merged with the top-level `snowflake.env_vars` block, which is separate because
Snowflake settings are not specific to this state machine — and because no
Snowflake detail is allowed anywhere in the compiled intake (SPEC §6.5). Those
variables carry secret IDs, never secret values.
"""

from typing import Any

from aws_cdk import Duration, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from cdk_common import add_state_machine
from constructs import Construct

# ASL has no substring intrinsic, and States.UUID() splits on "-" into groups of
# 8, 4, 4, 4 and 12 characters — no combination of them sums to 11, so the
# 12-character group is taken whole. That is why the run id is 13 characters
# (r + 12) rather than the 12 SPEC §6.3 describes; hex is a subset of the
# base32 charset both Lambdas accept.
RUN_ID_EXPRESSION = (
    "States.Format('r{}', "
    "States.ArrayGetItem(States.StringSplit(States.UUID(), '-'), 4))"
)

# The state fields the definition reads.
RUN_ID_PATH = "$.run_id"
# Stage 1 receives the execution input whole and resolves the feature intake from
# it — by model and feature group, or by directory URI
PLATFORM_PATH = "$.platform"
COMPILED_INTAKE_PATH = "$.compiled_intake_s3_uri"
FEATURE_GROUP_NAME_PATH = "$.feature_group_name"
# What stage 1 returns so stage 4 does not have to be re-derived from config: the
# form's chosen compute, and the ingest role AC-4 requires the job to run as.
INGEST_COMPUTE_PATH = "$.ingest_compute"
INGEST_ROLE_ARN_PATH = "$.ingest_role_arn"
RECREATE_PATH = "$.recreate"
# Where each stage 2 invocation's payload is collected beside the state.
CREATE_GROUP_PATH = "$.create_group"
DOCUMENT_PATH = "$.document"
# LambdaInvoke wraps a handler's return value in Payload.
LAMBDA_PAYLOAD_PATH = "$.Payload"

# Stage 4's own state, all under one key so the describe response sits beside the
# job name it describes.
INGEST_JOB_PATH = "$.ingest_job"
INGEST_JOB_NAME_PATH = f"{INGEST_JOB_PATH}.name"
INGEST_JOB_ARGUMENTS_PATH = f"{INGEST_JOB_PATH}.arguments"
INGEST_JOB_CREATED_PATH = f"{INGEST_JOB_PATH}.created"
INGEST_JOB_DESCRIBE_PATH = f"{INGEST_JOB_PATH}.describe"
INGEST_JOB_STATUS_PATH = f"{INGEST_JOB_DESCRIBE_PATH}.ProcessingJobStatus"

FEATURE_ONBOARDING_KEY = "feature_onboarding"

INGEST_JOB_KIND = "fi"


def _interpolate(env_vars, app_prefix, environment):
    """{app_prefix}/{env} interpolation, as the `lambdas` block uses."""
    return {
        key: value.replace("{app_prefix}", app_prefix).replace("{env}", environment)
        for key, value in env_vars.items()
    }


class CdkFeatureOnboardingStack(Stack):
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

        sm_config = var_config.get("state_machines", {}).get(FEATURE_ONBOARDING_KEY)
        if not sm_config:
            # Only dev.json is wired; qa and prod deploy nothing here.
            return

        name = sm_config["name"]
        state_machine_name = f"{app_prefix}-{name}-{environment}"

        # ── The two Lambda stages, named as cdk_lambdas.py names them ────
        compiler = lambda_.Function.from_function_arn(
            self,
            "FeatureIntakeCompilerFunction",
            f"arn:aws:lambda:{region}:{account}:function:"
            f"{app_prefix}-{sm_config['compiler_lambda']}-{environment}",
        )
        creater = lambda_.Function.from_function_arn(
            self,
            "FeatureGroupCreaterFunction",
            f"arn:aws:lambda:{region}:{account}:function:"
            f"{app_prefix}-{sm_config['creater_lambda']}-{environment}",
        )

        mint_run_id = sfn.Pass(
            self,
            "MintRunId",
            parameters={
                "generated_run_id": sfn.JsonPath.string_at(RUN_ID_EXPRESSION),
            },
            result_path=PLATFORM_PATH,
        )

        # ── The stages ───────────────────────────────────────────────────
        # Compile's return value is the state, because everything the later
        # states read is in it
        compile_intake = tasks.LambdaInvoke(
            self,
            "Compile",
            lambda_function=compiler,
            # The whole execution input, so stage 1 resolves the shape itself.
            payload=sfn.TaskInput.from_json_path_at("$"),
            output_path=LAMBDA_PAYLOAD_PATH,
        )

        # The two stage 2 invocations are collected BESIDE the state rather than
        # replacing it
        create_group = tasks.LambdaInvoke(
            self,
            "CreateGroup",
            lambda_function=creater,
            payload=sfn.TaskInput.from_object(
                {
                    "run_id": sfn.JsonPath.string_at(RUN_ID_PATH),
                    "compiled_intake_s3_uri": sfn.JsonPath.string_at(
                        COMPILED_INTAKE_PATH
                    ),
                    "action": "create",
                    "recreate": sfn.JsonPath.string_at(RECREATE_PATH),
                }
            ),
            result_selector={"payload": sfn.JsonPath.object_at(LAMBDA_PAYLOAD_PATH)},
            result_path=CREATE_GROUP_PATH,
        )

        # ── The wait loop ────────────────────────────────────────────────
        describe = tasks.CallAwsService(
            self,
            "DescribeFeatureGroup",
            service="sagemaker",
            action="describeFeatureGroup",
            # Without this CDK derives "sagemaker:describeFeatureGroup" from the
            # action name.
            iam_action="sagemaker:DescribeFeatureGroup",
            iam_resources=[
                f"arn:aws:sagemaker:{region}:{account}:feature-group/ar-*-{environment}-fgr-*"
            ],
            parameters={
                "FeatureGroupName": sfn.JsonPath.string_at(FEATURE_GROUP_NAME_PATH)
            },
            result_path="$.describe",
        )

        wait_for_created = sfn.Wait(
            self,
            "WaitForCreated",
            time=sfn.WaitTime.duration(Duration.seconds(sm_config["wait_seconds"])),
        )

        create_failed = sfn.Fail(
            self,
            "FeatureGroupCreateFailed",
            error="FeatureGroupCreateFailed",
            # SageMaker's own reason
            cause_path="$.describe.FailureReason",
        )

        document = tasks.LambdaInvoke(
            self,
            "Document",
            lambda_function=creater,
            payload=sfn.TaskInput.from_object(
                {
                    "run_id": sfn.JsonPath.string_at(RUN_ID_PATH),
                    "compiled_intake_s3_uri": sfn.JsonPath.string_at(
                        COMPILED_INTAKE_PATH
                    ),
                    "action": "document",
                }
            ),
            result_selector={"payload": sfn.JsonPath.object_at(LAMBDA_PAYLOAD_PATH)},
            result_path=DOCUMENT_PATH,
        )

        check_status = (
            sfn.Choice(self, "CheckFeatureGroupStatus")
            .when(
                sfn.Condition.string_equals("$.describe.FeatureGroupStatus", "Created"),
                document,
            )
            .when(
                sfn.Condition.string_equals(
                    "$.describe.FeatureGroupStatus", "Creating"
                ),
                wait_for_created,
            )
            # CreateFailed, and any other status a group can hold once creation
            # has been issued, carry a FailureReason.
            .otherwise(create_failed)
        )

        wait_for_created.next(describe)

        # ── Stage 4: the transform + ingestion Processing job ─────────────
        ingest_config = sm_config["ingest"]
        network = var_config["network"]
        processing_job_arn = f"arn:aws:sagemaker:{region}:{account}:processing-job/fi-*"

        # The job's environment. Nothing here is a credential: the two Snowflake
        # secret variables carry IDs the job resolves itself (SPEC §6.5).
        ingest_environment = {
            **_interpolate(ingest_config["env_vars"], app_prefix, environment),
            **_interpolate(
                var_config["snowflake"]["env_vars"], app_prefix, environment
            ),
        }

        # SPEC §6.2: the six mandatory cost-allocation tags, which the app-wide
        # CdkTaggingAspect cannot reach on a job created at run time, plus the
        # ar:runid tag of SPEC §6.3.
        ingest_tags = [
            {"Key": key, "Value": value}
            for key, value in _interpolate(
                var_config["tags"], app_prefix, environment
            ).items()
        ]
        ingest_tags.append(
            {"Key": "ar:runid", "Value": sfn.JsonPath.string_at(RUN_ID_PATH)}
        )

        # fi-<run id>
        ingest_job_name_expression = (
            f"States.Format('{INGEST_JOB_KIND}-{{}}', {RUN_ID_PATH})"
        )

        # The job name and the container arguments are built once, here, because
        # CreateIngestJob and DescribeIngestJob must agree on the name
        resolve_ingest_job = sfn.Pass(
            self,
            "ResolveIngestJob",
            parameters={
                "name": sfn.JsonPath.string_at(ingest_job_name_expression),
                "arguments": sfn.JsonPath.array(
                    "--compiled-intake-s3-uri",
                    sfn.JsonPath.string_at(COMPILED_INTAKE_PATH),
                    "--run-id",
                    sfn.JsonPath.string_at(RUN_ID_PATH),
                ),
            },
            result_path=INGEST_JOB_PATH,
        )

        create_ingest = tasks.CallAwsService(
            self,
            "CreateIngestJob",
            service="sagemaker",
            action="createProcessingJob",
            iam_action="sagemaker:CreateProcessingJob",
            iam_resources=[processing_job_arn],
            parameters={
                "ProcessingJobName": sfn.JsonPath.string_at(INGEST_JOB_NAME_PATH),
                "AppSpecification": {
                    # The image the compiler chose
                    "ImageUri": sfn.JsonPath.string_at(
                        f"{INGEST_COMPUTE_PATH}.image_uri"
                    ),
                    # No ContainerEntrypoint: the image bakes one in, so there is
                    # a single definition of how the job starts.
                    "ContainerArguments": sfn.JsonPath.list_at(
                        INGEST_JOB_ARGUMENTS_PATH
                    ),
                },
                "ProcessingResources": {
                    "ClusterConfig": {
                        "InstanceType": sfn.JsonPath.string_at(
                            f"{INGEST_COMPUTE_PATH}.instance_type"
                        ),
                        "InstanceCount": sfn.JsonPath.number_at(
                            f"{INGEST_COMPUTE_PATH}.instance_count"
                        ),
                        # Not in the form, so not in the document: the volume is
                        # platform-governed.
                        "VolumeSizeInGB": ingest_config["volume_size_gb"],
                    }
                },
                # Snowflake is reached over PrivateLink, which resolves to a
                # private address only from inside the VPC.
                "NetworkConfig": {
                    "VpcConfig": {
                        "SecurityGroupIds": network["security_groups"][
                            ingest_config["security_group"]
                        ],
                        "Subnets": network["subnet_groups"][
                            ingest_config["subnet_group"]
                        ],
                    }
                },
                # AC-4: the ingest role, taken from the document rather than
                # resolved here, which is why the compiler puts it there.
                "RoleArn": sfn.JsonPath.string_at(INGEST_ROLE_ARN_PATH),
                "Environment": ingest_environment,
                "StoppingCondition": {
                    "MaxRuntimeInSeconds": ingest_config["max_runtime_seconds"]
                },
                "Tags": ingest_tags,
            },
            result_path=INGEST_JOB_CREATED_PATH,
        )

        # Polled the same way stage 3 polls the feature group
        # (SDK returns on creation, not on job being done)
        describe_ingest = tasks.CallAwsService(
            self,
            "DescribeIngestJob",
            service="sagemaker",
            action="describeProcessingJob",
            iam_action="sagemaker:DescribeProcessingJob",
            iam_resources=[processing_job_arn],
            parameters={
                "ProcessingJobName": sfn.JsonPath.string_at(INGEST_JOB_NAME_PATH)
            },
            result_path=INGEST_JOB_DESCRIBE_PATH,
        )

        wait_for_ingest = sfn.Wait(
            self,
            "WaitForIngest",
            time=sfn.WaitTime.duration(Duration.seconds(sm_config["wait_seconds"])),
        )

        ingest_complete = sfn.Succeed(self, "IngestComplete")

        ingest_failed = sfn.Fail(
            self,
            "IngestJobFailed",
            error="IngestJobFailed",
            # The whole describe response
            cause_path=f"States.JsonToString({INGEST_JOB_DESCRIBE_PATH})",
        )

        check_ingest_status = (
            sfn.Choice(self, "CheckIngestJobStatus")
            .when(
                sfn.Condition.string_equals(INGEST_JOB_STATUS_PATH, "Completed"),
                ingest_complete,
            )
            .when(
                sfn.Condition.string_equals(INGEST_JOB_STATUS_PATH, "InProgress"),
                wait_for_ingest,
            )
            .when(
                sfn.Condition.string_equals(INGEST_JOB_STATUS_PATH, "Stopping"),
                wait_for_ingest,
            )
            # Failed, Stopped, and any status a job gains later.
            .otherwise(ingest_failed)
        )

        wait_for_ingest.next(describe_ingest)

        definition = mint_run_id
        mint_run_id.next(compile_intake)
        compile_intake.next(create_group).next(describe).next(check_status)
        document.next(resolve_ingest_job).next(create_ingest).next(
            describe_ingest
        ).next(check_ingest_status)

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
