"""CdkModelReadyDatasetStack — the NOVOS AR model-ready-dataset state machine.

NTU-1038 / AR-18-b-2. The Standard workflow that mints a run id, compiles the
model-ready-dataset intake form, and then runs the three Processing stages in order:

    MintRunId -> Validate (stage 1, the mrd-validation Lambda)
      -> ResolvePitJoinJob  -> CreatePitJoinJob  -> Describe/Check -> ...
      -> ResolveSplitJob    -> CreateSplitJob    -> Describe/Check -> ...
      -> ResolveValidateJob -> CreateValidateJob -> Describe/Check
             Completed             -> ModelReadyDatasetComplete
             InProgress / Stopping -> WaitFor<Stage> -> Describe<Stage>Job
             anything else         -> <Stage>JobFailed

**Three Processing stages, one image.** The image bakes `pit_join.py` as its
ENTRYPOINT, so every CreateProcessingJob sets `ContainerEntrypoint` explicitly —
including the PIT join's. Relying on the default would run stage 2's script for all
three (SPEC-M4 §4.5).

Each wait loop is a native `aws-sdk` integration rather than a Lambda: it runs in the
Step Functions service instead of the VPC, so it needs no SageMaker VPC endpoint and
no extra image. It has to be a loop because `aws_stepfunctions_tasks` carries no
Processing job construct at all, so there is no `.sync` integration and
`createProcessingJob` returns as soon as the job is created.

**No stage needs anything passed between states beyond the compiled intake's URI and
the run id.** `mrd_compiler` fixes every output location — `jobs.<stage>.output_s3_uri`,
`validation.report_s3_uri`, `storage.dataset_s3_uri` and `storage.dataset_latest_s3_uri`
— at compile time, before the first job runs. That is also why the jobs take no input
or output channels: they read and write with boto3 so every S3 call carries
`ExpectedBucketOwner`, which a `ProcessingInput` download cannot.

Execution input — `population_labeled_s3_uri` names the upstream cohort-label run and
is always required; `run_id` is always optional; the intake is named EITHER by model,
OR by the staged directory's S3 URI:

    {
      "model_id":   "cosentyx/patient_outcome_mortality",
      "population_labeled_s3_uri":
          "s3://<cohort-label bucket>/<upstream run>/population-label/"
          "population_labeled.parquet",
      "run_id":     "r5f3a9c2d1e0b",              # optional
      "as_of":      "2026-09-15T00:00:00Z"        # optional, defaults to now
    }

    {
      "model_ready_dataset_intake_s3_uri":
          "s3://<config bucket>/model-ready-dataset-intake/<brand>/<model>/",
      "population_labeled_s3_uri": "s3://...population_labeled.parquet",
      "run_id":     "r5f3a9c2d1e0b"               # optional
    }

Config in environments/<env>.json under "state_machines":

    "model_ready_dataset": {
      "name": "model-ready-dataset",
      "description": "...",
      "role": "{account_role_prefix}{app_prefix}-ModelReadyDatasetStateMachine",
      "validator_lambda": "mrd-validation",
      "wait_seconds": 30,
      "timeout_minutes": 240,
      "log_retention": "ONE_MONTH",
      "pit_join": { "volume_size_gb": 30, "max_runtime_seconds": 5400,
                    "subnet_group": "routable", "security_group": "lambda",
                    "env_vars": { "ENV": "{env}", ... } },
      "split":    { ... },
      "validate": { ... }
    }

`name` is the short form: the full state machine name is composed here as
`{app_prefix}-{name}-{environment}`, as cdk_lambdas.py composes function names.

Each job's name, image, instance type, instance count, role and TAGS are deliberately
NOT config: they come from the validator's return value, which reads them off the
compiled intake. That is what keeps the state machine from disagreeing with the
document — including the 63-character job-name assertion (V26) and `ar:owner`, both of
which stage 1 has already resolved. Only what the intake form has no field for — the
volume, the runtime cap, the subnets and the security group — is configured here.

Each stage's `env_vars` is merged with the top-level `snowflake.env_vars` block.
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

MODEL_READY_DATASET_KEY = "model_ready_dataset"

# Where the state machine puts what it minted, beside the caller's own input.
PLATFORM_PATH = "$.platform"
# LambdaInvoke wraps a handler's return value in Payload.
LAMBDA_PAYLOAD_PATH = "$.Payload"

# The validator's return value IS the state after Validate, because everything the
# later states read is in it.
RUN_ID_PATH = "$.run_id"
COMPILED_INTAKE_PATH = "$.compiled_intake_s3_uri"
# The form's chosen compute, shared by all three stages.
COMPUTE_PATH = "$.compute"

# Where the entrypoint scripts live in the image, as its Dockerfile copies them.
SCRIPT_DIR = "/novos/src/processing/model-ready-dataset"

# The three stages, in the order they run
STAGES = (
    ("pit_join", "PitJoin", "pit_join.py"),
    ("split", "Split", "split.py"),
    ("validate", "ValidateAndWrite", "validate_and_write.py"),
)


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


class CdkModelReadyDatasetStack(Stack):
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

        sm_config = var_config.get("state_machines", {}).get(MODEL_READY_DATASET_KEY)
        if not sm_config:
            # Only dev.json is wired; qa and prod deploy nothing here.
            return

        name = sm_config["name"]
        state_machine_name = f"{app_prefix}-{name}-{environment}"

        # ── Stage 1: the intake compiler, named as cdk_lambdas.py names it ──
        validator = lambda_.Function.from_function_arn(
            self,
            "MrdValidationFunction",
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

        validate_intake = tasks.LambdaInvoke(
            self,
            "Validate",
            lambda_function=validator,
            # The whole execution input, so stage 1 resolves the shape itself —
            # by model, or by staged directory URI.
            payload=sfn.TaskInput.from_json_path_at("$"),
            output_path=LAMBDA_PAYLOAD_PATH,
        )

        network = var_config["network"]
        processing_job_arn = f"arn:aws:sagemaker:{region}:{account}:processing-job/md-*"
        replacements = {
            "{app_prefix}": app_prefix,
            "{env}": environment,
            "{account}": account,
            "{region}": region,
        }

        complete = sfn.Succeed(self, "ModelReadyDatasetComplete")

        # Built back to front so each stage can be chained onto the next: the last
        # stage's success is `complete`, and every earlier one's is the next stage.
        next_state = complete
        for stage_key, stage_id, script in reversed(STAGES):
            next_state = self._stage(
                stage_key=stage_key,
                stage_id=stage_id,
                script=script,
                job_config=sm_config[stage_key],
                sm_config=sm_config,
                network=network,
                processing_job_arn=processing_job_arn,
                snowflake_env_vars=var_config["snowflake"]["env_vars"],
                replacements=replacements,
                on_success=next_state,
            )

        definition = mint_run_id
        mint_run_id.next(validate_intake).next(next_state)

        # ── The state machine (shared tail) ───────────────────────────────
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

    def _stage(
        self,
        *,
        stage_key,
        stage_id,
        script,
        job_config,
        sm_config,
        network,
        processing_job_arn,
        snowflake_env_vars,
        replacements,
        on_success,
    ):
        """One Processing stage: resolve -> create -> describe -> check -> wait loop.

        Returns the state to transition INTO, so the caller chains stages without
        knowing how many states each is made of. `on_success` is where a Completed
        job goes — the next stage, or the terminal Succeed.
        """
        # Everything the compiled intake already decided for this stage. Read, never
        # rebuilt: the job name carries the compiler's 63-character assertion (V26)
        # and the role is CohortLabelExecution, not the broad dev execution role.
        job_block = f"$.jobs.{stage_key}"
        job_name_path = f"{job_block}.processing_job_name"

        # This stage's own state, all under one key so the describe response sits
        # beside the job name it describes.
        state_path = f"$.{stage_key}_job"
        arguments_path = f"{state_path}.arguments"
        describe_path = f"{state_path}.describe"
        status_path = f"{describe_path}.ProcessingJobStatus"

        # Nothing here is a credential: the two Snowflake secret variables carry ARNs
        # the job resolves itself (SPEC-M4 §6.5).
        job_environment = {
            **_interpolate(job_config["env_vars"], replacements),
            **_interpolate(snowflake_env_vars, replacements),
        }

        # The container arguments are built once, here, so Create and Describe agree.
        # The job NAME is not built: it comes from the compiled intake.
        resolve_job = sfn.Pass(
            self,
            f"Resolve{stage_id}Job",
            parameters={
                "arguments": sfn.JsonPath.array(
                    "--compiled-intake-s3-uri",
                    sfn.JsonPath.string_at(COMPILED_INTAKE_PATH),
                    "--run-id",
                    sfn.JsonPath.string_at(RUN_ID_PATH),
                ),
            },
            result_path=state_path,
        )

        create_job = tasks.CallAwsService(
            self,
            f"Create{stage_id}Job",
            service="sagemaker",
            action="createProcessingJob",
            iam_action="sagemaker:CreateProcessingJob",
            iam_resources=[processing_job_arn],
            parameters={
                # Composed and length-asserted by stage 1 (V26).
                "ProcessingJobName": sfn.JsonPath.string_at(job_name_path),
                "AppSpecification": {
                    "ImageUri": sfn.JsonPath.string_at(f"{COMPUTE_PATH}.image_uri"),
                    # REQUIRED on every stage, including the PIT join: one image
                    # serves all three and its baked ENTRYPOINT is pit_join.py, so
                    # the default would run stage 2's script three times.
                    "ContainerEntrypoint": ["python3", f"{SCRIPT_DIR}/{script}"],
                    "ContainerArguments": sfn.JsonPath.list_at(arguments_path),
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
                # The offline store is read over the Feature Store / Athena path and
                # a snowflake-sourced cohort over PrivateLink, both of which resolve
                # to private addresses only from inside the VPC.
                "NetworkConfig": {
                    "VpcConfig": {
                        "SecurityGroupIds": network["security_groups"][
                            job_config["security_group"]
                        ],
                        "Subnets": network["subnet_groups"][job_config["subnet_group"]],
                    }
                },
                # CohortLabelExecution, taken from the document rather than resolved
                # here, which is why the compiler puts it there.
                "RoleArn": sfn.JsonPath.string_at(f"{job_block}.role_arn"),
                "Environment": job_environment,
                "StoppingCondition": {
                    "MaxRuntimeInSeconds": job_config["max_runtime_seconds"]
                },
                # SPEC-M4 §6.2's six mandatory cost-allocation tags plus ar:runid and
                # ar:owner, all already resolved by `mrd_compiler.run_tags`. Taken
                # from the document rather than rebuilt from the `tags` block, so
                # there is one definition and ar:owner is not silently dropped.
                "Tags": sfn.JsonPath.list_at(f"{job_block}.tags"),
            },
            result_path=f"{state_path}.created",
        )

        describe_job = tasks.CallAwsService(
            self,
            f"Describe{stage_id}Job",
            service="sagemaker",
            action="describeProcessingJob",
            iam_action="sagemaker:DescribeProcessingJob",
            iam_resources=[processing_job_arn],
            parameters={"ProcessingJobName": sfn.JsonPath.string_at(job_name_path)},
            result_path=describe_path,
        )

        wait_for_job = sfn.Wait(
            self,
            f"WaitFor{stage_id}",
            time=sfn.WaitTime.duration(Duration.seconds(sm_config["wait_seconds"])),
        )

        job_failed = sfn.Fail(
            self,
            f"{stage_id}JobFailed",
            error=f"{stage_id}JobFailed",
            # The whole describe response, which carries SageMaker's FailureReason.
            # For the validate stage that is how a DatasetRejected surfaces — the
            # report and the SNS alert are the job's own doing, not the machine's.
            cause_path=f"States.JsonToString({describe_path})",
        )

        check_status = (
            sfn.Choice(self, f"Check{stage_id}JobStatus")
            .when(sfn.Condition.string_equals(status_path, "Completed"), on_success)
            .when(sfn.Condition.string_equals(status_path, "InProgress"), wait_for_job)
            .when(sfn.Condition.string_equals(status_path, "Stopping"), wait_for_job)
            # Failed, Stopped, and any status a job gains later.
            .otherwise(job_failed)
        )

        wait_for_job.next(describe_job)
        resolve_job.next(create_job).next(describe_job).next(check_status)
        return resolve_job
