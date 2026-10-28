"""CdkModelTrainingStateMachineStack  — the NOVOS AR model-training state machine.

The three-state workflow behind src/pipelines/model_training.py (the two Lambdas
plus a native wait):

    Validate (Lambda) ─ valid? ─┬─ false ─> ValidationFailed
                                └─ true  ─> Launch (Lambda, start=true)
      -> DescribePipelineExecution -> CheckPipelineStatus
             Succeeded            -> PipelineSucceeded
             Executing / Stopping -> WaitForPipeline -> DescribePipelineExecution
             anything else        -> PipelineFailed

The launch Lambda upserts AND starts the SageMaker Pipeline itself (start=true) and
returns the execution ARN; State 3 is a native `aws-sdk` describe/poll loop over
that ARN — it runs in the Step Functions service, not the VPC, so it needs no
SageMaker VPC endpoint and no container. (There is no `.sync` pipeline task
construct, and the repo's feature-onboarding machine polls the same way.)

Execution input — `config_s3_uri` required, `onboarding_form_s3_uri` optional:

    {
      "config_s3_uri":          "s3://.../model-training-config.yaml",
      "onboarding_form_s3_uri": "s3://.../model-onboarding-form.yaml"
    }

Config in environments/<env>.json under state_machines.model_training:

    "model_training": {
      "name": "model-training",
      "validate_lambda": "model-training-pipeline-validate",
      "launch_lambda": "model-training-pipeline-launch",
      "role": "{account_role_prefix}{app_prefix}-statemachine-model-training",
      "wait_seconds": 60,
      "timeout_hours": 24,
      "log_retention": "ONE_MONTH"
    }

`name` is the short form: the full state machine name is composed here as
`{app_prefix}-{name}-{environment}`, as cdk_lambdas.py composes function names.

`validate_lambda` / `launch_lambda` are keys of the `lambdas` block, whose function
names cdk_lambdas.py composes the same way. The role comes from IamRoles; the two
invoked functions from Lambdas.
"""

from typing import Any

from aws_cdk import Duration, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from cdk_common import add_state_machine
from constructs import Construct

# State fields the definition reads.
CONFIG_S3_URI_PATH = "$.config_s3_uri"
ONBOARDING_FORM_S3_URI_PATH = "$.onboarding_form_s3_uri"
VALID_PATH = "$.valid"
EXECUTION_ARN_PATH = "$.execution_arn"
LAMBDA_PAYLOAD_PATH = "$.Payload"
PIPELINE_STATUS_PATH = "$.describe.PipelineExecutionStatus"


class CdkModelTrainingStateMachineStack(Stack):
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

        sm_config = (var_config.get("state_machines") or {}).get("model_training")
        if not sm_config:
            # Only wired where the model_training block is present.
            return

        state_machine_name = f"{app_prefix}-{sm_config['name']}-{environment}"

        # ── The two Lambda stages, named as cdk_lambdas.py names them ────
        validate = lambda_.Function.from_function_arn(
            self,
            "ModelTrainingValidateFunction",
            f"arn:aws:lambda:{region}:{account}:function:"
            f"{app_prefix}-{sm_config['validate_lambda']}-{environment}",
        )
        launch = lambda_.Function.from_function_arn(
            self,
            "ModelTrainingLaunchFunction",
            f"arn:aws:lambda:{region}:{account}:function:"
            f"{app_prefix}-{sm_config['launch_lambda']}-{environment}",
        )

        # ── State 1: Validate ────────────────────────────────────────────
        # The whole execution input is sent as the event, so config_s3_uri and the
        # optional onboarding_form_s3_uri reach the handler's event.get() as-is.
        # The return value becomes the state.
        validate_task = tasks.LambdaInvoke(
            self,
            "Validate",
            lambda_function=validate,
            output_path=LAMBDA_PAYLOAD_PATH,
        )

        validation_failed = sfn.Fail(
            self,
            "ValidationFailed",
            error="ValidationFailed",
            cause_path="$.error",
        )

        # ── State 2: Build & Launch (start=true) ─────────────────────────
        launch_task = tasks.LambdaInvoke(
            self,
            "Launch",
            lambda_function=launch,
            payload=sfn.TaskInput.from_object(
                {
                    "config_s3_uri": sfn.JsonPath.string_at(CONFIG_S3_URI_PATH),
                    "onboarding_form_s3_uri": sfn.JsonPath.string_at(
                        ONBOARDING_FORM_S3_URI_PATH
                    ),
                    "start": True,
                }
            ),
            output_path=LAMBDA_PAYLOAD_PATH,
        )

        # ── State 3: describe/poll the pipeline execution ────────────────
        describe = tasks.CallAwsService(
            self,
            "DescribePipelineExecution",
            service="sagemaker",
            action="describePipelineExecution",
            # Without this CDK derives "sagemaker:describePipelineExecution".
            iam_action="sagemaker:DescribePipelineExecution",
            iam_resources=[
                f"arn:aws:sagemaker:{region}:{account}:pipeline/*/execution/*"
            ],
            parameters={
                "PipelineExecutionArn": sfn.JsonPath.string_at(EXECUTION_ARN_PATH)
            },
            result_path="$.describe",
        )

        wait_for_pipeline = sfn.Wait(
            self,
            "WaitForPipeline",
            time=sfn.WaitTime.duration(Duration.seconds(sm_config["wait_seconds"])),
        )

        pipeline_succeeded = sfn.Succeed(self, "PipelineSucceeded")

        pipeline_failed = sfn.Fail(
            self,
            "PipelineFailed",
            error="PipelineFailed",
            cause_path="States.JsonToString($.describe)",
        )

        check_status = (
            sfn.Choice(self, "CheckPipelineStatus")
            .when(
                sfn.Condition.string_equals(PIPELINE_STATUS_PATH, "Succeeded"),
                pipeline_succeeded,
            )
            .when(
                sfn.Condition.string_equals(PIPELINE_STATUS_PATH, "Executing"),
                wait_for_pipeline,
            )
            .when(
                sfn.Condition.string_equals(PIPELINE_STATUS_PATH, "Stopping"),
                wait_for_pipeline,
            )
            # Failed, Stopped, and any status the execution gains later.
            .otherwise(pipeline_failed)
        )

        wait_for_pipeline.next(describe)

        check_valid = (
            sfn.Choice(self, "CheckValid")
            .when(sfn.Condition.boolean_equals(VALID_PATH, True), launch_task)
            .otherwise(validation_failed)
        )

        validate_task.next(check_valid)
        launch_task.next(describe).next(check_status)

        # ── The state machine (shared tail; logical IDs preserved) ────────
        add_state_machine(
            self,
            id_prefix="ModelTrainingStateMachine",
            state_machine_name=state_machine_name,
            definition=validate_task,
            role_config=sm_config["role"],
            account_role_prefix=account_role_prefix,
            app_prefix=app_prefix,
            environment=environment,
            ssm_prefix="model-training",
            timeout=Duration.hours(sm_config["timeout_hours"]),
            comment=sm_config.get("description", "NOVOS AR model training"),
            log_retention=sm_config["log_retention"],
        )
