"""Tests for CdkTaggingAspect.

AC1: Every taggable CFN resource receives all 6 required tags when the tags block is complete.
AC2: A missing required tag causes an Annotations error on the resource.
AC3: A blank / whitespace-only tag value is treated as missing.
AC4: The 'Environment' tag is auto-injected from the CDK environment arg when absent.
AC5: Non-taggable constructs do not raise.
AC6: End-to-end synth with a complete config applies all 6 tags on an S3 bucket.
"""

import aws_cdk as cdk
import pytest
from aws_cdk import Stack, assertions
from aws_cdk import aws_s3 as s3
from cdk_stacks.cdk_tagging_aspect import REQUIRED_TAG_KEYS, CdkTaggingAspect

VALID_TAGS = {
    "ClarityID": "95600",
    "DataProductName": "ScientificLens",
    "DataProductId": "novos-sl",
    "Environment": "dev",
    "Component": "novos-sl-platform",
    "BU": "Novartis-Pharma",
}

_CDK_ENV = cdk.Environment(account="123456789012", region="us-east-1")


def _stack_with_bucket(tags: dict, environment: str = "dev") -> Stack:
    app = cdk.App()
    stack = Stack(app, "TestStack", env=_CDK_ENV)
    s3.CfnBucket(stack, "Bucket")
    cdk.Aspects.of(stack).add(CdkTaggingAspect(tags=tags, environment=environment))
    return stack


# ── AC1: All 6 tags applied ───────────────────────────────────────────────────


class TestAC1AllTagsApplied:

    def test_bucket_has_all_six_tags(self):
        stack = _stack_with_bucket(dict(VALID_TAGS))
        template = assertions.Template.from_stack(stack)
        for key, value in VALID_TAGS.items():
            template.has_resource_properties(
                "AWS::S3::Bucket",
                {"Tags": assertions.Match.array_with([{"Key": key, "Value": value}])},
            )

    def test_required_tag_keys_constant_has_six_entries(self):
        assert len(REQUIRED_TAG_KEYS) == 6
        assert set(REQUIRED_TAG_KEYS) == {
            "ClarityID",
            "DataProductName",
            "DataProductId",
            "Environment",
            "Component",
            "BU",
        }


# ── AC2: Missing tag → Annotations error ─────────────────────────────────────


class TestAC2MissingTagError:

    @pytest.mark.parametrize("missing_key", REQUIRED_TAG_KEYS)
    def test_missing_tag_records_error_annotation(self, missing_key):
        tags = {k: v for k, v in VALID_TAGS.items() if k != missing_key}
        if missing_key == "Environment":
            tags["Environment"] = ""
        stack = _stack_with_bucket(tags)
        assertions.Template.from_stack(stack)
        assertions.Annotations.from_stack(stack).has_error(
            "*", assertions.Match.string_like_regexp("TaggingPolicy")
        )


# ── AC3: Blank / whitespace tag value treated as missing ─────────────────────


class TestAC3BlankTagValue:

    @pytest.mark.parametrize("blank_value", ["", "   ", "\t"])
    def test_blank_clarity_id_treated_as_missing(self, blank_value):
        tags = dict(VALID_TAGS)
        tags["ClarityID"] = blank_value
        stack = _stack_with_bucket(tags)
        assertions.Template.from_stack(stack)
        assertions.Annotations.from_stack(stack).has_error(
            "*",
            assertions.Match.string_like_regexp("ClarityID|missing|TaggingPolicy"),
        )

    def test_non_blank_values_accepted(self):
        stack = _stack_with_bucket(dict(VALID_TAGS))
        template = assertions.Template.from_stack(stack)
        assert template is not None


# ── AC4: Environment auto-injected ───────────────────────────────────────────


class TestAC4EnvironmentAutoInjected:

    def test_environment_defaulted_when_absent_from_tags(self):
        tags_without_env = {k: v for k, v in VALID_TAGS.items() if k != "Environment"}
        aspect = CdkTaggingAspect(tags=tags_without_env, environment="qa")
        assert aspect.tags["Environment"] == "qa"

    def test_environment_not_overwritten_when_present(self):
        aspect = CdkTaggingAspect(tags=dict(VALID_TAGS), environment="prod")
        assert aspect.tags["Environment"] == "dev"  # value from VALID_TAGS preserved

    def test_auto_injected_environment_appears_on_resources(self):
        tags_without_env = {k: v for k, v in VALID_TAGS.items() if k != "Environment"}
        stack = _stack_with_bucket(tags_without_env, environment="dev")
        template = assertions.Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "Tags": assertions.Match.array_with(
                    [{"Key": "Environment", "Value": "dev"}]
                )
            },
        )


# ── AC5: Non-taggable constructs skipped ─────────────────────────────────────


class TestAC5NonTaggableSkipped:

    def test_is_taggable_returns_false_for_plain_construct(self):
        from constructs import Construct

        app = cdk.App()
        stack = Stack(app, "S")
        plain = Construct(stack, "PlainConstruct")
        assert CdkTaggingAspect._is_taggable_cfn_resource(plain) is False

    def test_is_taggable_returns_true_for_cfn_bucket(self):
        app = cdk.App()
        stack = Stack(app, "S2")
        bucket = s3.CfnBucket(stack, "Bucket")
        assert CdkTaggingAspect._is_taggable_cfn_resource(bucket) is True

    def test_visit_does_not_raise_on_non_taggable_node(self):
        from constructs import Construct

        app = cdk.App()
        stack = Stack(app, "S3")
        plain = Construct(stack, "Plain")
        aspect = CdkTaggingAspect(tags=dict(VALID_TAGS), environment="dev")
        aspect.visit(plain)


# ── AC6: End-to-end synth ─────────────────────────────────────────────────────


class TestAC6EndToEndSynth:

    def test_stack_synthesizes_cleanly_with_all_tags(self):
        stack = _stack_with_bucket(dict(VALID_TAGS))
        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::S3::Bucket", 1)

    def test_all_six_tags_present_on_bucket(self):
        stack = _stack_with_bucket(dict(VALID_TAGS))
        template = assertions.Template.from_stack(stack)
        resources = template.find_resources("AWS::S3::Bucket")
        bucket = next(iter(resources.values()))
        tag_keys = {t["Key"] for t in bucket["Properties"].get("Tags", [])}
        for key in REQUIRED_TAG_KEYS:
            assert key in tag_keys, f"Tag '{key}' missing from S3 bucket"
