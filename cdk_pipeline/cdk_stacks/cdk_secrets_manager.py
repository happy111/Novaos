"""CdkSecretsManagerStack — Secrets Manager secrets for the NOVOS SL platform.

Secret names follow the pattern: {app_prefix}-{secret_key}-{environment}

Secret ARNs are published to SSM:
  - /{app_prefix}/{env}/secrets/{secret_key}-arn

Secrets are encrypted with the DDIT-provided CMK from config["kms_key_arn"].
Tags are applied globally by CdkTaggingAspect in app.py.
"""

from typing import Any

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_kms as kms
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class CdkSecretsManagerStack(Stack):
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
        secret_keys = var_config.get("secret_manager", [])

        if not secret_keys:
            return

        # ── Import DDIT CMK (static ARN — SSM tokens not supported in KMS ref) ──
        cmk = kms.Key.from_key_arn(self, "CmkKey", var_config["kms_key_arn"])

        removal_policy = (
            RemovalPolicy.DESTROY if environment == "dev" else RemovalPolicy.RETAIN
        )

        last_secret = None

        for secret_key in secret_keys:
            secret_name = f"{app_prefix}-{secret_key}-{environment}"

            secret = secretsmanager.Secret(
                self,
                f"{app_prefix}{secret_key}Secret",
                secret_name=secret_name,
                description=f"Secret for {app_prefix}-{secret_key}",
                encryption_key=cmk,
                removal_policy=removal_policy,
            )

            # Publish ARN to SSM so downstream stacks reference by ARN, not value
            ssm.StringParameter(
                self,
                f"{secret_key}Arn",
                parameter_name=f"/{app_prefix}/{environment}/secrets/{secret_key}-arn",
                string_value=secret.secret_arn,
                description=f"ARN of {secret_key} secret",
            )

            last_secret = secret

        # Output the last secret ARN for quick reference post-deploy
        if last_secret:
            CfnOutput(self, "LastSecretArn", value=last_secret.secret_arn)
