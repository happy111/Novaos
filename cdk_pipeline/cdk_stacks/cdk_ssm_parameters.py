"""CdkSsmParametersStack — SSM Parameter Store parameters for the NOVOS SL platform.

Parameter names follow the pattern: /{app_prefix}/{environment}/{service}/{key}

Values are driven by the "ssm_parameters" block in environments/<env>.json:
  {
    "ssm_parameters": {
      "snowflake": { "account": "...", "warehouse": "...", ... },
      "someother":  { "key": "value", ... }
    }
  }

Adding a new service requires only a new entry in the JSON — no code changes.
Tags are applied globally by CdkTaggingAspect in app.py.
"""

from typing import Any

from aws_cdk import Stack
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class CdkSsmParametersStack(Stack):
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
        ssm_config = var_config.get("ssm_parameters", {})

        for service, params in ssm_config.items():
            for key, value in params.items():
                construct_id = f"{service.title()}{key.title()}Param"
                parameter_name = f"/{app_prefix}/{environment}/{service}/{key}"

                ssm.StringParameter(
                    self,
                    construct_id,
                    parameter_name=parameter_name,
                    string_value=value,
                    description=f"{service} {key}",
                    tier=ssm.ParameterTier.STANDARD,
                )

        kms_key_arn = var_config.get("kms_key_arn")
        if kms_key_arn:
            ssm.StringParameter(
                self,
                "KmsKeyArnParam",
                parameter_name=f"/{app_prefix}/{environment}/kms-key-arn",
                string_value=kms_key_arn,
                description="KMS key ARN",
                tier=ssm.ParameterTier.STANDARD,
            )
