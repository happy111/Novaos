"""Tests for CdkLambdasStack."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from cdk_stacks.cdk_lambdas import CdkLambdasStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
APP_PREFIX = "novos-ar"


def _lambda_config(environment: str, lambdas: dict = None) -> dict:
    return {
        environment: {
            "app_prefix": APP_PREFIX,
            "account": ACCOUNT,
            "region": REGION,
            "account_role_prefix": f"RRDRAR65{environment.upper()}_AWS_APP01-",
            "ecr_repo_name": "f1ai/applications/pwsa0002037",
            "network": {
                "vpc_id": "vpc-0abc123def456",
                "subnet_groups": {
                    "routable": ["subnet-aaa111", "subnet-bbb222"],
                    "non-routable": ["subnet-ccc333", "subnet-ddd444"],
                },
                "security_groups": {
                    "lambda": ["sg-222bbb"],
                },
            },
            "lambdas": lambdas or {},
        }
    }


@pytest.fixture
def lambda_template(monkeypatch):
    monkeypatch.setenv("NEPTUNE_HC_IMAGE_TAG", "ar-neptune-check-abc1234-dev")
    app = cdk.App()
    config = _lambda_config(
        "dev",
        lambdas={
            "neptune_check": {
                "description": "Neptune health check - dummy test",
                "timeout": 1,
                "memory": 256,
                "subnet_group": "routable",
                "role": "{account_role_prefix}{app_prefix}-lambda-execution",
                "env_vars": {"APP_PREFIX": "{app_prefix}", "ENV": "{env}"},
            }
        },
    )
    stack = CdkLambdasStack(
        scope=app,
        id="TestLambdas",
        environment="dev",
        config=config,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack)


@pytest.fixture
def empty_lambda_template():
    app = cdk.App()
    config = _lambda_config("dev", lambdas={})
    stack = CdkLambdasStack(
        scope=app,
        id="TestLambdasEmpty",
        environment="dev",
        config=config,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack)


def test_lambda_function_created(lambda_template):
    # 1 application function + 1 CDK-managed singleton custom-resource function
    # that LogRetention uses to call the CloudWatch Logs API.
    lambda_template.resource_count_is("AWS::Lambda::Function", 2)


def test_lambda_function_name(lambda_template):
    lambda_template.has_resource_properties(
        "AWS::Lambda::Function",
        {"FunctionName": f"{APP_PREFIX}-neptune_check-dev"},
    )


def test_lambda_uses_ecr_image(lambda_template):
    functions = lambda_template.find_resources("AWS::Lambda::Function")
    found = any("f1ai/applications/pwsa0002037" in str(fn) for fn in functions.values())
    assert found, "Lambda function does not reference the expected ECR repo"


def test_lambda_timeout(lambda_template):
    lambda_template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Timeout": 60},
    )


def test_lambda_memory(lambda_template):
    lambda_template.has_resource_properties(
        "AWS::Lambda::Function",
        {"MemorySize": 256},
    )


def test_lambda_in_vpc(lambda_template):
    # Only our application functions set an explicit FunctionName; the
    # LogRetention singleton does not, and is never placed in the VPC.
    functions = lambda_template.find_resources("AWS::Lambda::Function")
    app_functions = [
        fn for fn in functions.values() if "FunctionName" in fn["Properties"]
    ]
    assert app_functions, "No application Lambda functions found"
    for fn in app_functions:
        assert (
            "VpcConfig" in fn["Properties"]
        ), "Lambda function is not attached to a VPC"


def test_lambda_env_vars_interpolated(lambda_template):
    lambda_template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": {
                    "APP_PREFIX": APP_PREFIX,
                    "ENV": "dev",
                }
            }
        },
    )


def test_lambda_ssm_arn_published(lambda_template):
    lambda_template.resource_count_is("AWS::SSM::Parameter", 1)
    lambda_template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": f"/{APP_PREFIX}/dev/lambda/neptune_check/function-arn"},
    )


def test_lambda_log_retention_configured(lambda_template):
    lambda_template.resource_count_is("Custom::LogRetention", 1)
    lambda_template.has_resource_properties(
        "Custom::LogRetention",
        {
            "LogGroupName": f"/aws/lambda/{APP_PREFIX}-neptune_check-dev",
            "RetentionInDays": 365,
        },
    )


def test_no_lambdas_creates_no_functions(empty_lambda_template):
    empty_lambda_template.resource_count_is("AWS::Lambda::Function", 0)
