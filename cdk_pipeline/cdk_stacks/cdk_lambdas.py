"""CdkLambdasStack — Data-driven Lambda deployment for NOVOS AR.

All Lambda config lives in environments/<env>.json under the "lambdas" block.
Each Lambda gets:
  - ECR image from the shared lambda-images repo (tag from env var)
  - VPC attachment (per-lambda subnet_group + security groups) if vpc: true
  - Per-lambda execution role (fallback to default if not specified)
  - Environment variables with {app_prefix}/{env} interpolation

Per-lambda config in environments/<env>.json:
  "lambdas": {
    "<name>": {
      "description": "...",
      "timeout": 30,
      "memory": 256,
      "vpc": true,
      "subnet_group": "routable",
      "role": "<iam-role-name>",
      "env_vars": { "KEY": "value" }
    }
  }

An optional top-level "lambda_env_defaults" block supplies environment variables shared by
every Lambda; a per-lambda "env_vars" entry of the same key wins. That keeps one copy of a
value several functions need.

Placeholders usable in any env var value, so a real account id, ARN, subnet or CMK never
appears anywhere but the env JSON — and where SSM already holds the value, not even there:

  {app_prefix} {env} {account} {region} {account_role_prefix}
  {resource_tags_json}          the mandatory `tags` block, as a JSON object string
  {subnet_ids:<group>}          comma-separated ids from network.subnet_groups.<group>
  {security_group_ids:<group>}  comma-separated ids from network.security_groups.<group>
  {ssm:/path/to/parameter}      resolved by CloudFormation at deploy time, not pasted here

Adding a new Lambda requires zero CDK code changes — just add an entry
to config.yaml (for build) and the lambdas block in each env JSON (for deploy).
"""

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from aws_cdk import Duration, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from .env_config import require_configured

_SSM_PLACEHOLDER = re.compile(r"\{ssm:(?P<path>/[^}]+)\}")
_SUBNETS_PLACEHOLDER = re.compile(r"\{subnet_ids:(?P<group>[^}]+)\}")
_SECURITY_GROUPS_PLACEHOLDER = re.compile(r"\{security_group_ids:(?P<group>[^}]+)\}")


class CdkLambdasStack(Stack):
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
        network = var_config["network"]

        # ── Validate config before constructing anything ─────────────────
        # CLAUDE.md §CDK rules: "environment configuration is validated before synthesis". This
        # runs before the VPC lookup deliberately: an `UNSET-` repository name otherwise
        # synthesizes into an ImageUri ECR cannot accept and fails only at deploy, and in an
        # environment whose VPC is uncached the lookup error would mask it entirely (F19).
        validated_ecr_repo_name = require_configured(
            var_config, environment, "ecr_repo_name"
        )

        # ── Shared infrastructure ────────────────────────────────────────
        vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=network["vpc_id"])

        subnet_groups = network["subnet_groups"]

        # All lambdas share the same SG — resolved once
        lambda_sg_ids = network["security_groups"]["lambda"]
        lambda_security_groups = [
            ec2.SecurityGroup.from_security_group_id(self, f"LambdaSg{i}", sg_id)
            for i, sg_id in enumerate(lambda_sg_ids)
        ]

        # Cache resolved subnet selections to avoid duplicate constructs
        _subnet_cache: dict[str, ec2.SubnetSelection] = {}

        # ECR repo — existing repo, full name validated at the top of this method
        ecr_repo_name = validated_ecr_repo_name
        ecr_repo = ecr.Repository.from_repository_name(self, "EcrRepo", ecr_repo_name)

        default_exec_role_name = (
            f"{var_config.get('account_role_prefix', '')}{app_prefix}-lambda-execution"
        )
        lambdas_config = var_config.get("lambdas", {})
        default_log_retention = var_config.get("lambda_log_retention", "ONE_YEAR")
        _role_cache: dict[str, iam.IRole] = {}

        # LogRetention's custom-resource singleton needs its own role. The
        # CloudFormation deploy role can't iam:CreateRole (DDIT-restricted), so it
        # must run as an existing role — reuse the default Lambda execution role,
        # which cdk_iam_deploy_roles.py already grants PutRetentionPolicy on
        # /aws/lambda/*. Passed explicitly to every LogRetention() call below so
        # the stack's singleton is deterministic regardless of iteration order.
        log_retention_role = iam.Role.from_role_name(
            self, "LogRetentionRole", default_exec_role_name, mutable=False
        )

        # Load tag_var mapping from config.yaml (build config, not env-specific)
        config_file = Path(__file__).parent.parent / "config.yaml"
        with open(config_file) as f:
            pipeline_config = yaml.safe_load(f)
        tag_var_by_name = {
            img["name"]: img["tag_var"] for img in pipeline_config["images"]
        }

        # ── Create each Lambda ───────────────────────────────────────────
        for name, lmb in lambdas_config.items():
            tag_env_var = tag_var_by_name.get(
                name, f"{name.upper().replace('-', '_')}_IMAGE_TAG"
            )
            image_tag = os.environ.get(tag_env_var, f"{name}-latest-{environment}")

            # Interpolate env vars — shared defaults first, per-lambda entries win
            merged_env_vars = {
                **var_config.get("lambda_env_defaults", {}),
                **lmb.get("env_vars", {}),
            }
            env_vars = {
                key: self._interpolate(str(value), environment, var_config)
                for key, value in merged_env_vars.items()
            }

            function_name = f"{app_prefix}-{name}-{environment}"
            construct_id = "".join(p.title() for p in name.split("-"))

            # Resolve execution role
            account_role_prefix = var_config.get("account_role_prefix", "")
            role_name = (
                lmb.get("role", default_exec_role_name)
                .replace("{account_role_prefix}", account_role_prefix)
                .replace("{app_prefix}", app_prefix)
            )
            if role_name not in _role_cache:
                role_construct_id = "".join(
                    p.title() for p in role_name.replace("_", "-").split("-")
                )
                _role_cache[role_name] = iam.Role.from_role_name(
                    self, f"{role_construct_id}Role", role_name, mutable=False
                )
            exec_role = _role_cache[role_name]

            # Resolve subnet group — all lambdas are always in the VPC
            subnet_group_name = lmb.get("subnet_group", "non-routable")
            if subnet_group_name not in _subnet_cache:
                _subnet_cache[subnet_group_name] = ec2.SubnetSelection(
                    subnets=[
                        ec2.Subnet.from_subnet_id(
                            self, f"Subnet{subnet_group_name}{i}", sid
                        )
                        for i, sid in enumerate(subnet_groups[subnet_group_name])
                    ]
                )

            # cdk.json sets @aws-cdk/aws-lambda:useCdkManagedLogGroup, so
            # DockerImageFunction auto-creates an AWS::Logs::LogGroup for the
            # function unless one is supplied. Every one of these functions has
            # already run at least once in AWS, so its log group already exists —
            # letting CDK create its own collides with "already exists". Importing
            # it here makes the function reference the existing group instead of
            # trying to create it.
            imported_log_group = logs.LogGroup.from_log_group_name(
                self, f"{construct_id}LogGroup", f"/aws/lambda/{function_name}"
            )

            fn = lambda_.DockerImageFunction(
                self,
                f"{construct_id}Function",
                function_name=function_name,
                description=lmb.get("description", name),
                code=lambda_.DockerImageCode.from_ecr(
                    repository=ecr_repo, tag_or_digest=image_tag
                ),
                timeout=Duration.minutes(lmb.get("timeout", 1)),
                memory_size=lmb.get("memory", 256),
                role=exec_role,
                vpc=vpc,
                vpc_subnets=_subnet_cache[subnet_group_name],
                security_groups=lambda_security_groups,
                environment=env_vars,
                log_group=imported_log_group,
            )

            # An imported log group can't have its retention set directly, so
            # LogRetention manages retention on the same group by name — it
            # creates the group if it's ever missing, or just sets retention if
            # it's already there (as it is here).
            logs.LogRetention(
                self,
                f"{construct_id}LogRetention",
                log_group_name=f"/aws/lambda/{function_name}",
                retention=logs.RetentionDays(
                    lmb.get("log_retention", default_log_retention)
                ),
                role=log_retention_role,
            )

            # Publish function ARN to SSM
            ssm.StringParameter(
                self,
                f"{construct_id}Arn",
                parameter_name=f"/{app_prefix}/{environment}/lambda/{name}/function-arn",
                string_value=fn.function_arn,
                description=f"ARN of {name} Lambda function",
            )

    def _interpolate(
        self, value: str, environment: str, var_config: dict[str, Any]
    ) -> str:
        """Resolve the placeholders documented in this module's docstring.

        Scalars come from the env JSON; `{ssm:...}` resolves at deploy time through a
        CloudFormation SSM parameter reference, so the value is never duplicated into config.
        """
        network = var_config.get("network", {})
        resolved = (
            value.replace("{app_prefix}", var_config["app_prefix"])
            .replace("{env}", environment)
            .replace("{account}", var_config["account"])
            .replace("{region}", var_config["region"])
            .replace("{account_role_prefix}", var_config.get("account_role_prefix", ""))
            .replace(
                "{resource_tags_json}",
                json.dumps(var_config.get("tags", {}), sort_keys=True),
            )
        )

        def _subnets(match: re.Match) -> str:
            group = match.group("group")
            return ",".join(network.get("subnet_groups", {})[group])

        def _security_groups(match: re.Match) -> str:
            group = match.group("group")
            return ",".join(network.get("security_groups", {})[group])

        def _ssm(match: re.Match) -> str:
            return ssm.StringParameter.value_for_string_parameter(
                self, match.group("path")
            )

        resolved = _SUBNETS_PLACEHOLDER.sub(_subnets, resolved)
        resolved = _SECURITY_GROUPS_PLACEHOLDER.sub(_security_groups, resolved)
        return _SSM_PLACEHOLDER.sub(_ssm, resolved)
