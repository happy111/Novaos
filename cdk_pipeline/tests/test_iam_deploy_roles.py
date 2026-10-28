"""Tests for CdkIamDeployRolesStack."""

import pytest
from aws_cdk.assertions import Template
from cdk_stacks.cdk_iam_deploy_roles import CdkIamDeployRolesStack


@pytest.fixture
def iam_template(dev_app, dev_config):
    stack = CdkIamDeployRolesStack(
        scope=dev_app,
        id="TestIamRoles",
        environment="dev",
        config=dev_config,
    )
    return Template.from_stack(stack)


@pytest.fixture
def app_prefix(dev_config):
    """The app_prefix dev_config actually carries, so expected names track it
    instead of drifting if the fixture's value ever changes."""
    return dev_config["dev"]["app_prefix"]


def test_all_roles_created(iam_template):
    iam_template.resource_count_is("AWS::IAM::Role", 13)


def test_all_roles_have_permissions_boundary(iam_template):
    roles = iam_template.find_resources("AWS::IAM::Role")
    for logical_id, role in roles.items():
        assert (
            "PermissionsBoundary" in role["Properties"]
        ), f"Role {logical_id} is missing PermissionsBoundary"


def test_cohort_label_execution_role_trust(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-CohortLabelExecution",
            "AssumeRolePolicyDocument": {
                "Statement": [{"Principal": {"Service": "sagemaker.amazonaws.com"}}]
            },
        },
    )


def test_statemachine_role_trust(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-CohortLabelStateMachine",
            "AssumeRolePolicyDocument": {
                "Statement": [{"Principal": {"Service": "states.amazonaws.com"}}]
            },
        },
    )


def test_featurestore_ingest_role_exists(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": (
                f"RRDRAR65DEV_AWS_APP01-{app_prefix}-workload-FeatureStore-IngestRole"
            ),
        },
    )


def test_studio_execution_role_exists(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-workload-studioexecution",
        },
    )


def test_pipeline_invoke_role_exists(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-workload-pipelineinvoke",
        },
    )


def test_pipeline_execution_role_exists(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": (
                f"RRDRAR65DEV_AWS_APP01-{app_prefix}-workload-pipelineexecution"
            ),
        },
    )


@pytest.fixture
def workload_execution_policy(iam_template):
    """The statements of the WorkloadPipelineExecution role policy."""
    matches = [
        policy
        for policy in iam_template.find_resources("AWS::IAM::Policy").values()
        if "workload-pipelineexecution-policy" in policy["Properties"]["PolicyName"]
    ]
    assert len(matches) == 1, "expected exactly one workload pipeline execution policy"
    return {
        s["Sid"]: s for s in matches[0]["Properties"]["PolicyDocument"]["Statement"]
    }


def test_the_execution_role_can_attach_jobs_to_the_vpc(workload_execution_policy):
    """Every Processor/Estimator model_training.py builds now carries a VPC network
    config; without this grant CreateProcessingJob/CreateTrainingJob is denied at
    the network layer -- confirmed against a real execution.
    """
    eni = workload_execution_policy["VpcNetworkInterfaces"]
    assert set(eni["Action"]) == {
        "ec2:CreateNetworkInterface",
        "ec2:CreateNetworkInterfacePermission",
        "ec2:DeleteNetworkInterface",
        "ec2:DeleteNetworkInterfacePermission",
        "ec2:DescribeDhcpOptions",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs",
    }
    assert eni["Resource"] == "*"


def test_all_policies_created(iam_template):
    iam_template.resource_count_is("AWS::IAM::Policy", 13)


def test_featurestore_read_arn_from_config(iam_template):
    """Ensure featurestore read ARN uses config value, not hardcoded DEV ARN."""
    policies = iam_template.find_resources("AWS::IAM::Policy")
    featurestore_arn = "arn:aws:iam::987654321098:role/RRDRAR66DEV_AWS_APP01-novos-central-featurestoreread"
    found = any(featurestore_arn in str(p) for p in policies.values())
    assert found, "FeatureStore read role ARN not found in any policy"


@pytest.fixture
def featurestore_read_policy_name(app_prefix):
    return f"PRDRAR65DEVAWSAPP01-{app_prefix}-workload-featurestore-read-policy"


@pytest.fixture
def featurestore_read_statements(iam_template, featurestore_read_policy_name):
    """Statements of the FeatureStoreReadOnly customer-managed policy, by SID."""
    policies = iam_template.find_resources(
        "AWS::IAM::ManagedPolicy",
        {"Properties": {"ManagedPolicyName": featurestore_read_policy_name}},
    )
    assert policies, "FeatureStoreReadOnly managed policy not found"
    document = next(iter(policies.values()))["Properties"]["PolicyDocument"]
    return {s["Sid"]: s for s in document["Statement"]}


def test_the_managed_policies_are_created(iam_template):
    """FeatureStoreReadOnly and DatasetWrite are customer-managed policies.

    AWS::IAM::ManagedPolicy is a different CFN type from the AWS::IAM::Policy
    inline policies counted by test_all_policies_created, so it needs its own
    count assertion. The permissions boundary is imported, not created here.
    """
    iam_template.resource_count_is("AWS::IAM::ManagedPolicy", 2)


def test_featurestore_read_policy_name(iam_template, featurestore_read_policy_name):
    iam_template.has_resource_properties(
        "AWS::IAM::ManagedPolicy",
        {
            "ManagedPolicyName": featurestore_read_policy_name,
        },
    )


def test_featurestore_read_policy_statement_sids(featurestore_read_statements):
    """Both read paths the M2 LLD grants a DS are present, and nothing else."""
    assert set(featurestore_read_statements) == {
        "FeatureStoreOnlineRead",
        "GlueCatalogRead",
        "AthenaOfflineStoreQuery",
        "S3OfflineStoreRead",
        "S3AthenaQueryResults",
        "KMSFeatureStoreRead",
    }


def test_featurestore_read_online_store_actions(featurestore_read_statements):
    statement = featurestore_read_statements["FeatureStoreOnlineRead"]
    assert statement["Action"] == [
        "sagemaker:GetRecord",
        "sagemaker:BatchGetRecord",
        "sagemaker:DescribeFeatureGroup",
    ]
    assert (
        statement["Resource"]
        == "arn:aws:sagemaker:us-east-1:123456789012:feature-group/*"
    )


def test_featurestore_read_glue_covers_the_per_feature_group_databases(
    featurestore_read_statements,
):
    """The offline databases cannot be enumerated at synth time."""
    statement = featurestore_read_statements["GlueCatalogRead"]
    assert statement["Resource"] == [
        "arn:aws:glue:us-east-1:123456789012:catalog",
        "arn:aws:glue:us-east-1:123456789012:database/*",
        "arn:aws:glue:us-east-1:123456789012:table/*",
    ]


def test_featurestore_read_s3_covers_the_platforms_own_offline_store(
    featurestore_read_statements, app_prefix
):
    """A platform feature group's offline store is in the offline-store bucket."""
    resources = featurestore_read_statements["S3OfflineStoreRead"]["Resource"]
    offline_bucket = f"novartis-123456789012-dev-us-east-1-{app_prefix}-offline-store"
    assert f"arn:aws:s3:::{offline_bucket}" in resources
    assert f"arn:aws:s3:::{offline_bucket}/*" in resources
    assert "arn:aws:s3:::sagemaker-us-east-1-123456789012" in resources


def test_featurestore_read_policy_grants_no_writes_except_athena_results(
    featurestore_read_statements, app_prefix
):
    """Strictly narrower than the ingest role — the only write is Athena's results.

    No sagemaker:PutRecord, no Snowflake secret read, no Glue write.
    """
    forbidden = {
        "sagemaker:PutRecord",
        "sagemaker:BatchWriteRecord",
        "secretsmanager:GetSecretValue",
        "glue:CreateDatabase",
        "glue:CreateTable",
        "glue:UpdateTable",
        "kms:Encrypt",
    }
    for sid, statement in featurestore_read_statements.items():
        actions = set(statement["Action"])
        assert not actions & forbidden, f"{sid} grants {actions & forbidden}"
        if sid != "S3AthenaQueryResults":
            assert "s3:PutObject" not in actions, f"{sid} grants s3:PutObject"

    # The prefix ATHENA_OUTPUT_S3_URI names
    athena_results = featurestore_read_statements["S3AthenaQueryResults"]
    assert athena_results["Resource"] == (
        f"arn:aws:s3:::novartis-123456789012-dev-us-east-1-{app_prefix}-mrd-datasets"
        "/athena-results/*"
    )


def test_featurestore_read_kms_scoped_to_local_account_key(
    featurestore_read_statements,
):
    """KMS is scoped to the DRA65 CMK only.

    Every resource this policy reads — feature groups, Glue catalog, the offline
    store bucket and the Athena results prefix — lives in the local DRA65
    account and is encrypted with the DRA65 CMK (M2 LLD §4). Cross-account
    central reads go through dra66_featurestore_read_role_arn, and the DRA66 key
    grant belongs to that assumed role, not to this one.
    """
    statement = featurestore_read_statements["KMSFeatureStoreRead"]
    assert statement["Resource"] == "arn:aws:kms:us-east-1:123456789012:key/test-key-id"
    assert "987654321098" not in str(statement), "DRA66 key must not be in scope"


def test_ssm_parameters_published(iam_template):
    iam_template.resource_count_is("AWS::SSM::Parameter", 15)


def test_execution_role_ssm_path(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": f"/{app_prefix}/dev/cohort-label-execution-role-arn",
        },
    )


def test_statemachine_role_ssm_path(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": f"/{app_prefix}/dev/cohort-label-statemachine-role-arn",
        },
    )


def test_lambda_execution_role_exists(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-lambda-execution",
            "AssumeRolePolicyDocument": {
                "Statement": [{"Principal": {"Service": "lambda.amazonaws.com"}}]
            },
        },
    )


def test_lambda_execution_role_ssm_path(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": f"/{app_prefix}/dev/lambda-execution-role-arn",
        },
    )


# ── Feature-ingestion state machine: one shared Lambda role ──────────────


@pytest.fixture
def feature_ingestion_policy(iam_template):
    """The statements of the shared feature-ingestion Lambda policy."""
    matches = [
        policy
        for policy in iam_template.find_resources("AWS::IAM::Policy").values()
        if "lambda-feature-ingestion-policy" in policy["Properties"]["PolicyName"]
    ]
    assert len(matches) == 1, "expected exactly one feature-ingestion Lambda policy"
    return {
        s["Sid"]: s for s in matches[0]["Properties"]["PolicyDocument"]["Statement"]
    }


def test_one_role_serves_every_lambda_stage(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-lambda-feature-ingestion",
            "AssumeRolePolicyDocument": {
                "Statement": [{"Principal": {"Service": "lambda.amazonaws.com"}}]
            },
        },
    )
    iam_template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": f"/{app_prefix}/dev/lambda-feature-ingestion-role-arn"},
    )


def test_the_role_grants_what_the_stages_call_and_nothing_wider(
    feature_ingestion_policy,
):
    statements = feature_ingestion_policy

    # Stage 2's SageMaker calls, scoped to this environment's groups.
    assert set(statements["ManageFeatureGroups"]["Action"]) == {
        "sagemaker:CreateFeatureGroup",
        "sagemaker:DescribeFeatureGroup",
        "sagemaker:DeleteFeatureGroup",
        "sagemaker:UpdateFeatureMetadata",
        # V17: a tagged CreateFeatureGroup fails without a tag grant.
        "sagemaker:AddTags",
        "sagemaker:ListTags",
    }
    assert statements["ManageFeatureGroups"]["Resource"] == (
        "arn:aws:sagemaker:us-east-1:123456789012:feature-group/ar-*-dev-fgr-*"
    )

    # CreateFeatureGroup passes the ingest role, and only that role.
    pass_role = statements["PassIngestRoleToFeatureStore"]
    assert pass_role["Condition"] == {
        "StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}
    }
    assert "WorkloadFeatureStoreIngestRole" in str(pass_role["Resource"])

    # A CMK-encrypted online store needs a grant, conditioned to AWS resources.
    assert statements["KmsGrantForFeatureStoreOnlineStore"]["Condition"] == {
        "Bool": {"kms:GrantIsForAWSResource": "true"}
    }

    # Logs are scoped to the two Lambda stages, not /aws/lambda/*. Stage 3 is an
    # aws-sdk integration in the state machine, so it has no log group here.
    logs = statements["CloudWatchLogs"]["Resource"]
    assert len(logs) == 4
    assert not any(r.endswith("/aws/lambda/*") for r in logs)
    assert not any("describe-feature-group" in r for r in logs)
    glue = statements["GlueCatalogForOfflineStore"]
    assert {"glue:GetDatabase", "glue:CreateDatabase", "glue:CreateTable"} <= set(
        glue["Action"]
    )
    assert "glue:DeleteTable" in glue["Action"]
    assert "glue:DeleteDatabase" not in glue["Action"]
    assert "arn:aws:glue:us-east-1:123456789012:catalog" in glue["Resource"]

    # Stage 4 ingests as WorkloadFeatureStoreIngestRole, so no Lambda stage needs
    # PutRecord, the Snowflake secret, or to launch a Processing job.
    granted = {
        action
        for s in statements.values()
        for action in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    }
    for forbidden in (
        "sagemaker:PutRecord",
        "sagemaker:BatchWriteRecord",
        "secretsmanager:GetSecretValue",
        "sagemaker:CreateProcessingJob",
        "s3:DeleteObject",
    ):
        assert forbidden not in granted


@pytest.fixture
def featurestore_ingest_policy(iam_template):
    """The statements of the WorkloadFeatureStoreIngestRole policy."""
    matches = [
        policy
        for policy in iam_template.find_resources("AWS::IAM::Policy").values()
        if "featurestore-ingest" in policy["Properties"]["PolicyName"]
    ]
    assert len(matches) == 1, "expected exactly one featurestore ingest policy"
    return {
        s["Sid"]: s for s in matches[0]["Properties"]["PolicyDocument"]["Statement"]
    }


def test_the_ingest_role_can_write_the_offline_store_bucket(
    featurestore_ingest_policy, app_prefix
):
    """CreateFeatureGroup calls GetBucketAcl as this role to validate the S3Uri.

    Regression: the grant pointed at the develop sample's sagemaker-* bucket, so a
    real create failed with "Invalid S3Uri provided" (403 on s3:GetBucketAcl).
    """
    statements = featurestore_ingest_policy
    bucket = f"novartis-123456789012-dev-us-east-1-{app_prefix}-offline-store"
    at_bucket = statements["S3OfflineStoreBucketAccess"]
    assert "s3:GetBucketAcl" in at_bucket["Action"]
    assert f"arn:aws:s3:::{bucket}" in at_bucket["Resource"]

    in_bucket = statements["S3OfflineStoreWrite"]
    assert "s3:PutObject" in in_bucket["Action"]
    assert f"arn:aws:s3:::{bucket}/*" in in_bucket["Resource"]


def test_the_ingest_role_can_do_what_the_processing_job_does(
    featurestore_ingest_policy, app_prefix
):
    """Stage 4 read its inputs, wrote its report, pulled its image and logged —
    none of which the role could do when it only had the Feature Store grants."""
    statements = featurestore_ingest_policy
    ingestion_bucket = (
        f"arn:aws:s3:::novartis-123456789012-dev-us-east-1-{app_prefix}"
        "-feature-ingestion/*"
    )

    # Both inputs — the compiled intake and the clipped SQL it references.
    read = statements["ReadCompiledIntakeAndClippedSql"]
    assert read["Action"] == "s3:GetObject"
    assert read["Resource"] == ingestion_bucket

    # AC-6: the report lands at ingest.output_s3_uri in the same bucket.
    assert statements["WriteIngestReport"]["Action"] == "s3:PutObject"
    assert statements["WriteIngestReport"]["Resource"] == ingestion_bucket

    # Without these the job cannot pull the image it is told to run.
    assert statements["EcrAuthToken"]["Action"] == "ecr:GetAuthorizationToken"
    assert set(statements["EcrImageAccess"]["Action"]) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }

    logs = statements["CloudWatchLogs"]
    assert {"logs:CreateLogStream", "logs:PutLogEvents"} <= set(logs["Action"])
    assert logs["Resource"] == (
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/sagemaker/ProcessingJobs:*"
    )


def test_the_ingest_role_can_resolve_a_column_derived_pattern_c_feature(
    featurestore_ingest_policy,
):
    """A column-derived Pattern C feature has no concrete name until the ingest
    job resolves it from its own already-fetched source DataFrame, at which
    point it calls UpdateFeatureGroup --feature-additions (batched at the API's
    100-item limit), waits on DescribeFeatureGroup for LastUpdateStatus to
    settle between batches, and calls UpdateFeatureMetadata to document what
    it added -- all three real API calls this role's own service principal
    must be allowed to make, not just the static-feature grants stage 2
    already carries.

    Regression (in two parts, both confirmed against real AWS): first
    UpdateFeatureGroup/UpdateFeatureMetadata were missing entirely
    (AccessDeniedException on UpdateFeatureGroup); once added,
    UpdateFeatureGroup itself succeeded but the very next call —
    DescribeFeatureGroup inside add_features' own wait loop — was denied,
    since this role had never been granted it at all (only
    FeatureOnboardingStateMachine, a different role, had
    DescribeFeatureGroupWaitLoop).
    """
    statements = featurestore_ingest_policy
    grant = statements["SageMakerFeatureStoreColumnDerivedExpansion"]
    assert set(grant["Action"]) == {
        "sagemaker:UpdateFeatureGroup",
        "sagemaker:UpdateFeatureMetadata",
        "sagemaker:DescribeFeatureGroup",
    }
    assert (
        grant["Resource"] == "arn:aws:sagemaker:us-east-1:123456789012:feature-group/*"
    )


def test_the_ingest_role_can_attach_the_job_to_the_vpc(featurestore_ingest_policy):
    eni = featurestore_ingest_policy["VpcNetworkInterfaces"]
    assert {
        "ec2:CreateNetworkInterface",
        "ec2:CreateNetworkInterfacePermission",
        "ec2:DeleteNetworkInterface",
        "ec2:DeleteNetworkInterfacePermission",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs",
    } <= set(eni["Action"])


# ── Feature onboarding state machine role ────────────────────────────────


@pytest.fixture
def feature_onboarding_policy(iam_template):
    """The statements of the feature onboarding state machine policy."""
    matches = [
        policy
        for policy in iam_template.find_resources("AWS::IAM::Policy").values()
        if "statemachine-feature-onboarding" in policy["Properties"]["PolicyName"]
    ]
    assert len(matches) == 1, "expected exactly one feature onboarding policy"
    return {
        s["Sid"]: s for s in matches[0]["Properties"]["PolicyDocument"]["Statement"]
    }


def test_feature_onboarding_statemachine_role_exists(iam_template, app_prefix):
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": (
                f"RRDRAR65DEV_AWS_APP01-{app_prefix}-statemachine-feature-onboarding"
            ),
            "AssumeRolePolicyDocument": {
                "Statement": [{"Principal": {"Service": "states.amazonaws.com"}}]
            },
        },
    )
    iam_template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": f"/{app_prefix}/dev/statemachine-feature-onboarding-role-arn"},
    )


def test_the_statemachine_role_can_describe_feature_groups_itself(
    feature_onboarding_policy,
):
    """The wait loop is a native aws-sdk call, so the role — not a Lambda — makes it."""
    describe = feature_onboarding_policy["DescribeFeatureGroupWaitLoop"]
    assert describe["Action"] == "sagemaker:DescribeFeatureGroup"
    assert describe["Resource"] == (
        "arn:aws:sagemaker:us-east-1:123456789012:feature-group/ar-*-dev-fgr-*"
    )


def test_the_statemachine_role_can_run_and_poll_the_ingest_job(
    feature_onboarding_policy,
):
    """Stage 4 is a native aws-sdk integration with no .sync, so the role creates
    the job, polls it, and can stop it — and passes it the ingest role (AC-4)."""
    run = feature_onboarding_policy["RunIngestProcessingJob"]
    assert {
        "sagemaker:CreateProcessingJob",
        "sagemaker:DescribeProcessingJob",
        "sagemaker:StopProcessingJob",
        # V17: a create carrying Tags fails without a tag grant.
        "sagemaker:AddTags",
    } == set(run["Action"])
    assert run["Resource"] == (
        "arn:aws:sagemaker:us-east-1:123456789012:processing-job/fi-*"
    )

    pass_role = feature_onboarding_policy["PassIngestRoleToProcessingJob"]
    assert pass_role["Action"] == "iam:PassRole"
    assert pass_role["Condition"] == {
        "StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}
    }
    assert "WorkloadFeatureStoreIngestRole" in str(pass_role["Resource"])


def test_the_statemachine_role_can_deliver_logs(feature_onboarding_policy):
    """V28: without these a Standard workflow logs nothing, and nothing warns you."""
    logs = feature_onboarding_policy["StepFunctionsCloudWatchLogs"]
    assert {
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:PutResourcePolicy",
        "logs:DescribeLogGroups",
    } <= set(logs["Action"])


# --- the cohort-label pipeline's three roles (AR-18-a-2 AC-2 / AC-8) ---------


@pytest.fixture
def cohort_label_bucket(app_prefix):
    return f"arn:aws:s3:::novartis-123456789012-dev-us-east-1-{app_prefix}-cohort-label"


@pytest.fixture
def config_bucket(app_prefix):
    return f"arn:aws:s3:::novartis-123456789012-dev-us-east-1-{app_prefix}-config"


def _policy_by_sid(iam_template, name_fragment):
    matches = [
        policy
        for policy in iam_template.find_resources("AWS::IAM::Policy").values()
        if name_fragment in policy["Properties"]["PolicyName"]
    ]
    assert len(matches) == 1, f"expected exactly one {name_fragment} policy"
    return {
        s["Sid"]: s for s in matches[0]["Properties"]["PolicyDocument"]["Statement"]
    }


@pytest.fixture
def cohort_label_execution_policy(iam_template, app_prefix):
    """The statements of the CohortLabelExecution inline policy."""
    return _policy_by_sid(iam_template, f"{app_prefix}-execution-policy")


@pytest.fixture
def cohort_label_statemachine_policy(iam_template, app_prefix):
    return _policy_by_sid(iam_template, f"{app_prefix}-statemachine-policy")


@pytest.fixture
def cohort_label_lambda_policy(iam_template, app_prefix):
    return _policy_by_sid(iam_template, f"{app_prefix}-lambda-cohort-label-policy")


def test_the_execution_role_can_read_its_inputs_and_write_its_outputs(
    cohort_label_execution_policy, cohort_label_bucket
):
    """In: [Run ID]/compile-output/{compiled_intake.json, population_source.sql}.
    Out: [Run ID]/population-label/ and the cohort-datasets/ destination — all in
    the one cohort-label bucket."""
    read = cohort_label_execution_policy["ReadCompiledIntakeAndSourceSql"]
    assert read["Action"] == "s3:GetObject"
    assert read["Resource"] == f"{cohort_label_bucket}/*"

    write = cohort_label_execution_policy["WriteLabelledCohortAndReport"]
    assert write["Action"] == "s3:PutObject"
    assert write["Resource"] == f"{cohort_label_bucket}/*"


def test_the_execution_role_can_list_the_upstream_cohort_prefix(
    cohort_label_execution_policy, cohort_label_bucket
):
    """The MRD pit-join stage reads the upstream cohort from a PREFIX"""
    statement = cohort_label_execution_policy["ListUpstreamCohortDataset"]
    assert statement["Action"] == ["s3:ListBucket", "s3:GetBucketLocation"]
    assert statement["Resource"] == cohort_label_bucket


def test_the_execution_role_can_read_the_snowflake_secrets(
    cohort_label_execution_policy,
):
    """Credentials come from Secrets Manager, never the document and never the image.
    Without this the job cannot connect at all."""
    statement = cohort_label_execution_policy["ReadSnowflakeSecret"]
    assert statement["Action"] == "secretsmanager:GetSecretValue"
    # Secrets Manager appends a 6-character suffix to every secret ARN.
    assert statement["Resource"] == [
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:f1ai-pwsa0002037-??????",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:f1ai-pwsa0002037-key-??????",
    ]


def test_the_execution_role_can_pull_its_own_image(cohort_label_execution_policy):
    assert (
        cohort_label_execution_policy["EcrAuthToken"]["Action"]
        == "ecr:GetAuthorizationToken"
    )
    image = cohort_label_execution_policy["EcrImageAccess"]
    assert "repository/f1ai/applications/pwsa0002037" in str(image["Resource"])


def test_the_execution_role_can_encrypt_what_it_writes(cohort_label_execution_policy):
    """The cohort and the report are written with SSE-KMS."""
    statement = cohort_label_execution_policy["KMSAccessDRA65"]
    assert {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"} <= set(
        statement["Action"]
    )


def test_the_dataset_grant_names_a_bucket_that_exists(
    cohort_label_execution_policy, app_prefix
):
    """cdk_s3_buckets.py puts the region in every bucket name."""
    statement = cohort_label_execution_policy["S3ReadWriteDatasetLocal"]
    for resource in statement["Resource"]:
        assert f"dev-us-east-1-{app_prefix}-datasets" in resource


def test_featurestore_read_is_granted_by_policy_attachment(iam_template, app_prefix):
    """AC-8: read via FeatureStoreRead by same-account IAM policy ATTACHMENT, not by
    copying its statements inline and not by a cross-account trust policy."""
    roles = iam_template.find_resources(
        "AWS::IAM::Role",
        {
            "Properties": {
                "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-CohortLabelExecution"
            }
        },
    )
    assert len(roles) == 1
    attached = next(iter(roles.values()))["Properties"]["ManagedPolicyArns"]
    # One entry is the permissions boundary; the other is FeatureStoreReadOnly.
    assert any("FeatureStoreReadOnlyPolicy" in str(arn) for arn in attached)


def test_the_statemachine_role_can_invoke_the_validator(
    cohort_label_statemachine_policy, app_prefix
):
    """Without this the state machine cannot run its first task at all."""
    statement = cohort_label_statemachine_policy["InvokeCohortValidationLambda"]
    assert statement["Action"] == "lambda:InvokeFunction"
    assert statement["Resource"] == (
        f"arn:aws:lambda:us-east-1:123456789012:function:{app_prefix}"
        "-cohort-validation-dev"
    )


def test_the_statemachine_role_is_scoped_to_its_own_kind_of_job(
    cohort_label_statemachine_policy,
):
    """AC-2, least privilege: not every processing job in the account. `cl` only, so
    this role cannot touch feature ingestion's `fi` jobs or the MRD `md` ones."""
    statement = cohort_label_statemachine_policy["StartSageMakerJobs"]
    assert statement["Resource"] == (
        "arn:aws:sagemaker:us-east-1:123456789012:processing-job/cl-*"
    )
    assert {
        "sagemaker:CreateProcessingJob",
        "sagemaker:DescribeProcessingJob",
        "sagemaker:StopProcessingJob",
        # V17: a create carrying Tags fails without a tag grant.
        "sagemaker:AddTags",
    } == set(statement["Action"])


def test_the_statemachine_pass_role_is_pinned_to_sagemaker(
    cohort_label_statemachine_policy,
):
    """So CohortLabelExecution cannot be handed to anything but SageMaker."""
    statement = cohort_label_statemachine_policy["PassExecutionRole"]
    assert statement["Action"] == "iam:PassRole"
    assert statement["Condition"] == {
        "StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}
    }
    assert "CohortLabelExecutionRole" in str(statement["Resource"])


def test_the_validator_lambda_role_exists(iam_template, app_prefix):
    """Referenced by lambdas.cohort-validation.role in environments/<env>.json, so
    without it the function cannot be deployed."""
    iam_template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-lambda-cohort-label",
            "AssumeRolePolicyDocument": {
                "Statement": [{"Principal": {"Service": "lambda.amazonaws.com"}}]
            },
        },
    )


def test_the_validator_lambda_reads_the_staged_intake_and_writes_the_document(
    cohort_label_lambda_policy, cohort_label_bucket, config_bucket
):
    assert (
        cohort_label_lambda_policy["ReadStagedIntakeDirectory"]["Resource"]
        == f"{config_bucket}/*"
    )
    # ListBucket so the handler can mirror a DIRECTORY rather than named objects.
    assert (
        cohort_label_lambda_policy["ListStagedIntakeDirectory"]["Resource"]
        == config_bucket
    )
    assert (
        cohort_label_lambda_policy["WriteCompiledIntake"]["Resource"]
        == f"{cohort_label_bucket}/*"
    )


def test_the_validator_lambda_is_narrower_than_the_ingestion_lambda(
    cohort_label_lambda_policy,
):
    """It creates no feature groups, touches no Glue catalog and passes no role."""
    granted = {
        action
        for statement in cohort_label_lambda_policy.values()
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    assert not [action for action in granted if action.startswith("glue:")]
    assert "iam:PassRole" not in granted
    assert not [action for action in granted if action.startswith("sagemaker:")]
    # And it never reads a Snowflake credential — only the Processing job does.
    assert "secretsmanager:GetSecretValue" not in granted


# --- the model-ready-dataset pipeline's roles (AR-18-b-2, NTU-1038) ----------


@pytest.fixture
def mrd_bucket(app_prefix):
    return f"arn:aws:s3:::novartis-123456789012-dev-us-east-1-{app_prefix}-mrd-datasets"


@pytest.fixture
def dataset_write_policy_name(app_prefix):
    return f"PRDRAR65DEVAWSAPP01-{app_prefix}-dataset-write-policy"


@pytest.fixture
def dataset_write_statements(iam_template, dataset_write_policy_name):
    """Statements of the DatasetWrite customer-managed policy, by SID."""
    policies = iam_template.find_resources(
        "AWS::IAM::ManagedPolicy",
        {"Properties": {"ManagedPolicyName": dataset_write_policy_name}},
    )
    assert policies, "DatasetWrite managed policy not found"
    document = next(iter(policies.values()))["Properties"]["PolicyDocument"]
    return {s["Sid"]: s for s in document["Statement"]}


@pytest.fixture
def mrd_statemachine_policy(iam_template, app_prefix):
    return _policy_by_sid(iam_template, f"{app_prefix}-mrd-statemachine-policy")


@pytest.fixture
def mrd_lambda_policy(iam_template, app_prefix):
    return _policy_by_sid(iam_template, f"{app_prefix}-lambda-mrd-policy")


def test_the_two_managed_policies_are_attached_to_the_execution_role(
    iam_template, app_prefix
):
    """SPEC-M4 §4.4 requires the offline-store read and the dataset write to be
    ATTACHED policies, same-account, not inline copies and not cross-account
    assumes. A policy that exists but is attached to nothing grants nothing."""
    roles = iam_template.find_resources(
        "AWS::IAM::Role",
        {
            "Properties": {
                "RoleName": f"RRDRAR65DEV_AWS_APP01-{app_prefix}-CohortLabelExecution"
            }
        },
    )
    assert len(roles) == 1
    attached = str(next(iter(roles.values()))["Properties"]["ManagedPolicyArns"])
    assert "FeatureStoreReadOnlyPolicy" in attached
    assert "DatasetWritePolicy" in attached


def test_dataset_write_covers_the_whole_dataset_area(
    dataset_write_statements, mrd_bucket
):
    """Every MRD stage both reads and writes this one bucket: the compiled intake,
    the Athena results, the pit-join and splits scratch, the report and the
    versioned dataset. AbortMultipartUpload is there because a parquet partition
    can exceed the single-PUT limit and a failed upload otherwise bills forever."""
    statement = dataset_write_statements["DatasetAreaReadWrite"]
    assert {
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:AbortMultipartUpload",
    } <= set(statement["Action"])
    assert statement["Resource"] == [mrd_bucket, f"{mrd_bucket}/*"]


def test_dataset_write_can_encrypt(dataset_write_statements):
    """The bucket denies a non-KMS put, so a write grant without GenerateDataKey
    fails outright rather than silently downgrading the encryption."""
    statement = dataset_write_statements["DatasetAreaKMS"]
    assert {"kms:Encrypt", "kms:GenerateDataKey", "kms:Decrypt"} <= set(
        statement["Action"]
    )
    assert statement["Resource"] == "arn:aws:kms:us-east-1:123456789012:key/test-key-id"


def test_the_mrd_statemachine_invokes_only_its_own_validator(
    mrd_statemachine_policy, app_prefix
):
    """Its own role rather than a second consumer of CohortLabelStateMachine — which
    is the whole point: it must not be able to invoke the other pipeline's Lambda."""
    invoke = mrd_statemachine_policy["InvokeMrdValidationLambda"]
    assert invoke["Resource"] == (
        f"arn:aws:lambda:us-east-1:123456789012:function:{app_prefix}"
        "-mrd-validation-dev"
    )
    assert "cohort-validation" not in str(mrd_statemachine_policy)


def test_the_mrd_statemachine_is_scoped_to_its_own_kind_of_job(mrd_statemachine_policy):
    """All three md-kind stages, and nothing else: not cohort-label's `cl` jobs, not
    feature ingestion's `fi` ones."""
    statement = mrd_statemachine_policy["StartSageMakerJobs"]
    assert statement["Resource"] == (
        "arn:aws:sagemaker:us-east-1:123456789012:processing-job/md-*"
    )


def test_the_mrd_statemachine_passes_the_m4_execution_role(mrd_statemachine_policy):
    """AC-2: all three jobs run as CohortLabelExecution, never the broad dev role."""
    pass_role = mrd_statemachine_policy["PassExecutionRole"]
    assert pass_role["Action"] == "iam:PassRole"
    assert "CohortLabelExecution" in str(pass_role["Resource"])
    assert pass_role["Condition"] == {
        "StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}
    }


def test_the_mrd_validator_lambda_can_reach_all_three_of_its_inputs(
    mrd_lambda_policy, cohort_label_bucket, config_bucket, mrd_bucket
):
    """It reads the staged form from the config bucket AND the upstream cohort-label
    run's compiled intake, then writes the compiled intake to the MRD bucket.
    CohortLabelLambda can do neither of the last two, which is why this role exists
    rather than being a second consumer of that one."""
    upstream = mrd_lambda_policy["ReadUpstreamCohortLabelRun"]
    assert set(upstream["Action"]) == {"s3:GetObject", "s3:ListBucket"}
    assert upstream["Resource"] == [cohort_label_bucket, f"{cohort_label_bucket}/*"]

    assert mrd_lambda_policy["ReadStagedIntakeDirectory"]["Resource"] == (
        f"{config_bucket}/*"
    )

    write = mrd_lambda_policy["WriteCompiledIntake"]
    assert write["Action"] == "s3:PutObject"
    assert write["Resource"] == f"{mrd_bucket}/*"


def test_the_mrd_validator_lambda_grants_no_compute_and_no_secrets(mrd_lambda_policy):
    """It validates and compiles; it never runs a job or reads a credential. The
    three Processing stages do that, as CohortLabelExecution."""
    granted = {
        action
        for statement in mrd_lambda_policy.values()
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    assert "iam:PassRole" not in granted
    assert "secretsmanager:GetSecretValue" not in granted
    assert not [action for action in granted if action.startswith("sagemaker:")]
    assert not [action for action in granted if action.startswith("athena:")]
