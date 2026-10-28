"""Tests for CdkS3BucketsStack."""

import json
import pathlib
import re

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from cdk_stacks.cdk_s3_buckets import CdkS3BucketsStack


def _make_config(environment="dev", bucket_names=None, extra_s3_cfg=None):
    s3_cfg = {
        "bucket_names": bucket_names if bucket_names is not None else ["datasets"]
    }
    if extra_s3_cfg:
        s3_cfg.update(extra_s3_cfg)
    return {
        environment: {
            "app_prefix": "nv-ar",
            "account": "111122223333",
            "region": "us-east-1",
            "kms_key_arn": "arn:aws:kms:us-east-1:111122223333:key/test-key-id",
            "s3_buckets": s3_cfg,
        }
    }


def _make_env(environment="dev"):
    return cdk.Environment(account="111122223333", region="us-east-1")


def _synth(config, environment="dev"):
    app = cdk.App()
    stack = CdkS3BucketsStack(
        app,
        "TestS3",
        environment=environment,
        config=config,
        env=_make_env(environment),
    )
    return assertions.Template.from_stack(stack)


class TestS3BucketCreation:

    def test_single_bucket_created(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        template.resource_count_is("AWS::S3::Bucket", 1)

    def test_multiple_buckets_created(self):
        template = _synth(_make_config(bucket_names=["datasets", "raw-landing"]))
        template.resource_count_is("AWS::S3::Bucket", 2)

    def test_bucket_name_includes_region(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {"BucketName": "novartis-111122223333-dev-us-east-1-nv-ar-datasets"},
        )

    def test_bucket_has_versioning_enabled(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {"VersioningConfiguration": {"Status": "Enabled"}},
        )

    def test_bucket_has_kms_encryption(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": [
                        {
                            "ServerSideEncryptionByDefault": {
                                "SSEAlgorithm": "aws:kms"
                            },
                            "BucketKeyEnabled": True,
                        }
                    ]
                }
            },
        )

    def test_bucket_blocks_public_access(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                }
            },
        )


class TestS3RemovalPolicy:

    def test_dev_removal_policy_destroy(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        template.has_resource(
            "AWS::S3::Bucket",
            {"DeletionPolicy": "Delete"},
        )

    def test_prod_removal_policy_retain(self):
        config = _make_config(environment="prod", bucket_names=["datasets"])
        template = _synth(config, environment="prod")  # noqa: E501
        template.has_resource(
            "AWS::S3::Bucket",
            {"DeletionPolicy": "Retain"},
        )


class TestS3SsmOutputs:

    def test_ssm_arn_published_per_bucket(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        template.has_resource_properties(
            "AWS::SSM::Parameter",
            {"Name": "/nv-ar/dev/s3/datasets-bucket-arn"},
        )

    def test_ssm_name_published_per_bucket(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        template.has_resource_properties(
            "AWS::SSM::Parameter",
            {"Name": "/nv-ar/dev/s3/datasets-bucket-name"},
        )

    def test_two_ssm_params_per_bucket(self):
        template = _synth(_make_config(bucket_names=["datasets", "raw-landing"]))
        template.resource_count_is("AWS::SSM::Parameter", 4)


class TestS3RawLandingPolicy:

    def test_raw_landing_has_deny_non_kms_puts(self):
        template = _synth(_make_config(bucket_names=["raw-landing"]))
        policies = template.find_resources("AWS::S3::BucketPolicy")
        assert any(
            "DenyNonKmsPuts" in str(p) for p in policies.values()
        ), "DenyNonKmsPuts policy not found on raw-landing bucket"

    def test_non_raw_landing_has_no_deny_policy(self):
        template = _synth(_make_config(bucket_names=["datasets"]))
        policies = template.find_resources("AWS::S3::BucketPolicy")
        assert not any(
            "DenyNonKmsPuts" in str(p) for p in policies.values()
        ), "DenyNonKmsPuts should not appear on non-raw-landing bucket"


class TestS3WorkloadReportStore:

    def test_report_store_bucket_policy_roles_applied(self):
        config = _make_config(
            bucket_names=["wl-report-store"],
            extra_s3_cfg={
                "wl_report_store": {
                    "bucket_policy_roles": ["arn:aws:iam::123456789012:role/test-role"]
                }
            },
        )
        template = _synth(config)
        policies = template.find_resources("AWS::S3::BucketPolicy")
        assert any(
            "test-role" in str(p) for p in policies.values()
        ), "Bucket policy role not found on wl-report-store"

    def test_report_store_no_policy_when_no_roles(self):
        template = _synth(_make_config(bucket_names=["wl-report-store"]))
        policies = template.find_resources("AWS::S3::BucketPolicy")
        assert not any(
            "AllowReportStoreAccess" in str(p) for p in policies.values()
        ), "Unexpected bucket policy on wl-report-store with no roles configured"


class TestEnvironmentBucketConfig:
    """The real environments/<env>.json bucket lists, not a synthetic config.

    S3 caps a bucket name at 63 characters and the name is composed from five
    parts, so a suffix that fits in dev can overflow in prod. Asserted here
    because the failure only surfaces at deploy time.
    """

    ENVIRONMENTS = ["dev", "qa", "prod"]
    REQUIRED_SUFFIXES = ["feature-ingestion", "offline-store"]

    def _env_config(self, environment):
        path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "environments"
            / f"{environment}.json"
        )
        return json.loads(path.read_text())

    @pytest.mark.parametrize("environment", ENVIRONMENTS)
    def test_feature_ingestion_buckets_are_declared(self, environment):
        names = self._env_config(environment)["s3_buckets"]["bucket_names"]
        for suffix in self.REQUIRED_SUFFIXES:
            assert suffix in names, f"{suffix} missing from {environment}.json"

    @pytest.mark.parametrize("environment", ENVIRONMENTS)
    def test_composed_bucket_names_fit_the_s3_limit(self, environment):
        config = self._env_config(environment)
        for suffix in config["s3_buckets"]["bucket_names"]:
            name = (
                f"novartis-{config['account']}-{environment}-"
                f"{config['region']}-{config['app_prefix']}-{suffix}"
            )
            assert len(name) <= 63, f"{name} is {len(name)} characters"
            assert re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", name), name
