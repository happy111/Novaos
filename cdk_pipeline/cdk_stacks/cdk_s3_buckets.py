"""CdkS3BucketsStack — Foundational S3 buckets for the NOVOS SL platform.

Creates multiple buckets from config-driven list:
  Bucket name pattern: {bucket_prefix}-{environment}-{region}-{bucket_name}

Raw landing bucket additionally gets:
  - Per-BU extraction roles: PutObject on {bu}/* prefix only
  - NeptuneLoader role: GetObject + ListBucket only

All buckets have:
  - SSE-KMS encryption (CMK ARN from config's kms_key_arn)
  - Block public access ON
  - Versioning enabled
  - TLS-only enforcement (enforce_ssl)

KMS grants (BU: Encrypt/GenerateDataKey, Neptune: Decrypt) are managed
directly on the CMK key policy — not via this stack.

Tags are applied globally by CdkTaggingAspect in app.py.
"""

from typing import Any

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class CdkS3BucketsStack(Stack):
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

        # ── S3 bucket config ─────────────────────────────────────────────
        s3_cfg = var_config["s3_buckets"]
        bucket_names = s3_cfg["bucket_names"]

        account = var_config["account"]
        region = var_config["region"]
        bucket_prefix = f"novartis-{account}"

        # # ── Raw landing config ───────────────────────────────────────────
        # raw_landing_cfg = s3_cfg["raw_landing"]
        # bu_roles = raw_landing_cfg.get("bu_extraction_roles", [])
        # neptune_loader_role_arn = raw_landing_cfg["neptune_loader_role_arn"]

        # ── KMS CMK ARN ───────────────────────────────────────────────────
        kms_key_arn = var_config["kms_key_arn"]

        # ── Import existing CMK ──────────────────────────────────────────
        cmk = kms.Key.from_key_arn(self, "CmkKey", kms_key_arn)

        removal_policy = (
            RemovalPolicy.DESTROY if environment == "dev" else RemovalPolicy.RETAIN
        )

        # ── Create all buckets ───────────────────────────────────────────
        for bucket_suffix in bucket_names:
            bucket_name = (
                f"{bucket_prefix}-{environment}-{region}-{app_prefix}-{bucket_suffix}"
            )

            # Per-bucket lifecycle rules from config
            bucket_cfg = s3_cfg.get(bucket_suffix.replace("-", "_"), {})
            lifecycle_rules = self._build_lifecycle_rules(
                bucket_cfg.get("lifecycle_rules", [])
            )

            bucket = s3.Bucket(
                self,
                f"{bucket_suffix.replace('-', '').title()}Bucket",
                bucket_name=bucket_name,
                versioned=True,
                encryption=s3.BucketEncryption.KMS,
                encryption_key=cmk,
                bucket_key_enabled=True,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                removal_policy=removal_policy,
                enforce_ssl=True,
                lifecycle_rules=lifecycle_rules or None,
            )

            # ── Raw landing specific policies ────────────────────────────
            # if bucket_suffix == "raw-landing":
            #     self._attach_raw_landing_policies(
            #         bucket, bu_roles, neptune_loader_role_arn
            #     )

            # ── WorkloadReportStore bucket policy roles ──────────────────
            if bucket_suffix == "wl-report-store":
                report_cfg = s3_cfg.get("wl_report_store", {})
                for role_arn in report_cfg.get("bucket_policy_roles", []):
                    bucket.add_to_resource_policy(
                        iam.PolicyStatement(
                            sid=f"AllowReportStoreAccess{role_arn.split('/')[-1].replace('-', '').replace('_', '')}",
                            effect=iam.Effect.ALLOW,
                            principals=[iam.ArnPrincipal(role_arn)],
                            actions=[
                                "s3:GetObject",
                                "s3:PutObject",
                                "s3:AbortMultipartUpload",
                                "s3:ListBucket",
                            ],
                            resources=[
                                bucket.bucket_arn,
                                f"{bucket.bucket_arn}/*",
                            ],
                        )
                    )

            # ── Deny non-KMS puts on raw-landing only ────────────────────
            if bucket_suffix == "raw-landing":
                bucket.add_to_resource_policy(
                    iam.PolicyStatement(
                        sid="DenyNonKmsPuts",
                        effect=iam.Effect.DENY,
                        principals=[iam.AnyPrincipal()],
                        actions=["s3:PutObject"],
                        resources=[f"{bucket.bucket_arn}/*"],
                        conditions={
                            "StringNotEqualsIfExists": {
                                "s3:x-amz-server-side-encryption": "aws:kms"
                            },
                            "Null": {"s3:x-amz-server-side-encryption": "false"},
                        },
                    )
                )

            # ── Publish bucket ARN and name to SSM ───────────────────────
            ssm.StringParameter(
                self,
                f"{bucket_suffix.replace('-', '').title()}BucketArn",
                parameter_name=f"/{app_prefix}/{environment}/s3/{bucket_suffix}-bucket-arn",
                string_value=bucket.bucket_arn,
                description=f"ARN of the {bucket_suffix} S3 bucket",
            )
            ssm.StringParameter(
                self,
                f"{bucket_suffix.replace('-', '').title()}BucketName",
                parameter_name=f"/{app_prefix}/{environment}/s3/{bucket_suffix}-bucket-name",
                string_value=bucket.bucket_name,
                description=f"Name of the {bucket_suffix} S3 bucket",
            )

    def _build_lifecycle_rules(self, rules_cfg: list) -> list:
        """Convert config lifecycle_rules list into CDK LifecycleRule objects."""
        rules = []
        for rule in rules_cfg:
            storage_class_map = {
                "GLACIER": s3.StorageClass.GLACIER,
                "GLACIER_IR": s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                "DEEP_ARCHIVE": s3.StorageClass.DEEP_ARCHIVE,
                "INTELLIGENT_TIERING": s3.StorageClass.INTELLIGENT_TIERING,
                "ONEZONE_IA": s3.StorageClass.ONE_ZONE_INFREQUENT_ACCESS,
                "STANDARD_IA": s3.StorageClass.INFREQUENT_ACCESS,
            }
            storage_class = storage_class_map.get(
                rule.get("transition_storage_class", "GLACIER"),
                s3.StorageClass.GLACIER,
            )
            rules.append(
                s3.LifecycleRule(
                    id=rule.get("id"),
                    prefix=rule.get("prefix"),
                    transitions=[
                        s3.Transition(
                            storage_class=storage_class,
                            transition_after=Duration.days(rule["transition_days"]),
                        )
                    ],
                    expiration=Duration.days(rule["expiration_days"]),
                )
            )
        return rules
