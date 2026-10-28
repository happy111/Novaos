"""Tests for CdkSecretsManagerStack."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from cdk_stacks.cdk_secrets_manager import CdkSecretsManagerStack

APP_PREFIX = "novos-ar"


def _secrets_config(environment: str, secret_keys: list = None) -> dict:
    return {
        environment: {
            "app_prefix": APP_PREFIX,
            "kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/test-key-id",
            "secret_manager": secret_keys or [],
        }
    }


@pytest.fixture
def secrets_template():
    app = cdk.App()
    config = _secrets_config(
        "dev", secret_keys=["collate", "centree", "sharepoint-details"]
    )
    stack = CdkSecretsManagerStack(
        scope=app,
        id="TestSecrets",
        environment="dev",
        config=config,
    )
    return Template.from_stack(stack)


@pytest.fixture
def empty_secrets_template():
    app = cdk.App()
    config = _secrets_config("dev", secret_keys=[])
    stack = CdkSecretsManagerStack(
        scope=app,
        id="TestSecretsEmpty",
        environment="dev",
        config=config,
    )
    return Template.from_stack(stack)


@pytest.fixture
def prod_secrets_template():
    app = cdk.App()
    config = _secrets_config("prod", secret_keys=["collate"])
    stack = CdkSecretsManagerStack(
        scope=app,
        id="TestSecretsProd",
        environment="prod",
        config=config,
    )
    return Template.from_stack(stack)


def test_secrets_created(secrets_template):
    secrets_template.resource_count_is("AWS::SecretsManager::Secret", 3)


def test_secret_name_follows_pattern(secrets_template):
    secrets_template.has_resource_properties(
        "AWS::SecretsManager::Secret",
        {"Name": f"{APP_PREFIX}-collate-dev"},
    )


def test_secret_has_kms_encryption(secrets_template):
    secrets = secrets_template.find_resources("AWS::SecretsManager::Secret")
    for secret in secrets.values():
        assert (
            "KmsKeyId" in secret["Properties"]
        ), "Secret is not encrypted with KMS key"


def test_ssm_parameters_published(secrets_template):
    secrets_template.resource_count_is("AWS::SSM::Parameter", 3)


def test_ssm_parameter_path(secrets_template):
    secrets_template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": f"/{APP_PREFIX}/dev/secrets/collate-arn"},
    )


def test_dev_removal_policy_destroy(secrets_template):
    secrets = secrets_template.find_resources("AWS::SecretsManager::Secret")
    for secret in secrets.values():
        assert (
            secret.get("DeletionPolicy") == "Delete"
        ), "Dev secret should have DESTROY removal policy"


def test_prod_removal_policy_retain(prod_secrets_template):
    secrets = prod_secrets_template.find_resources("AWS::SecretsManager::Secret")
    for secret in secrets.values():
        assert (
            secret.get("DeletionPolicy") == "Retain"
        ), "Prod secret should have RETAIN removal policy"


def test_empty_secret_keys_creates_nothing(empty_secrets_template):
    empty_secrets_template.resource_count_is("AWS::SecretsManager::Secret", 0)
    empty_secrets_template.resource_count_is("AWS::SSM::Parameter", 0)


def test_cfn_output_for_last_secret(secrets_template):
    secrets_template.has_output(
        "LastSecretArn",
        {},
    )
