# Shared test fixtures
import aws_cdk as cdk
import pytest


def _base_config(environment: str) -> dict:
    """Build a minimal but complete config dict for unit tests."""
    return {
        environment: {
            "app_prefix": "novos-ar",
            "stack_prefix": "APP01-NVSGISRDRAR65",
            "account": "123456789012",
            "region": "us-east-1",
            "account_role_prefix": f"RRDRAR65{environment.upper()}_AWS_APP01-",
            "policy_prefix": f"PRDRAR65{environment.upper()}AWSAPP01",
            "permissions_boundary": "arn:aws:iam::123456789012:policy/PRDRAR65AWSIAMPERMBOUNDARYV1ID01",
            "kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/test-key-id",
            "kms_key_arn_dra66": "arn:aws:kms:us-east-1:987654321098:key/test-dra66-key-id",
            "dra66_dataset_write_role_arn": "arn:aws:iam::987654321098:role/RRDRAR66DEV_AWS_APP01-novos-central-datasetwrite",
            "dra66_featurestore_read_role_arn": "arn:aws:iam::987654321098:role/RRDRAR66DEV_AWS_APP01-novos-central-featurestoreread",
            "roles": {
                "appadm_role": "RIDY_AWS_RDRAR65_APPADMV1",
                "dpadmin_role": "RRDRAR65DEV_AWS_APP01-novos-DataAdmin_Role-dev",
                "cdk_deploy_role": "RRDRAR65_AWS_APP01-cdk-hnb659fds-deploy-role-us-east-1",
                "cdk_cfn_exec_role": "RRDRAR65_AWS_APP01-cdk-hnb659fds-cfn-exec-role-us-east-1",
                "cdk_lookup_role": "RRDRAR65_AWS_APP01-cdk-hnb659fds-lookup-role-us-east-1",
                "cdk_file_asset_publishing_role": "RRDRAR65_AWS_APP01-cdk-hnb659fds-file-publishing-role-us-east-1",
                "cdk_image_asset_publishing_role": "RRDRAR65_AWS_APP01-cdk-hnb659fds-image-role-us-east-1",
            },
            "network": {
                "vpc_id": "vpc-0abc123def456",
                "subnet_groups": {
                    "routable": ["subnet-aaa111", "subnet-bbb222"],
                    "non-routable": ["subnet-ccc333", "subnet-ddd444"],
                },
                "security_groups": {
                    "neptune": ["sg-111aaa"],
                    "lambda": ["sg-222bbb"],
                },
            },
            "ecr_repo_names": ["lambda-images", "sagemaker"],
            "ecr_repo_name": "f1ai/applications/pwsa0002037",
            "s3_buckets": {
                "bucket_names": ["datasets", "test-bucket", "wl-report-store"],
            },
            "secret_manager": ["collate", "centree", "sharepoint-details"],
            "jenkins": {
                "external_id": "12345",
                "user_id": "app_t1_88899_uc0022",
                "assume_role_name": "RIDY_AWS_RDRAR65_APPADMV1",
                "ridy_identity_acc_role_name": "RIDY_AWS_RDRAR65_APPADM",
            },
            "notifications": {
                "sns_topics": [{"name": "ds-alert", "display_name": "DS Alert"}],
                "sqs_queues": [],
            },
            "tags": {
                "ClarityID": "96354",
                "DataProductName": "NOVOS",
                "DataProductId": "novos",
                "Environment": environment,
                "Component": "novos-ar-platform",
                "BU": "Novartis-Pharma",
            },
        }
    }


@pytest.fixture
def dev_config():
    return _base_config("dev")


@pytest.fixture
def prod_config():
    return _base_config("prod")


@pytest.fixture
def dev_app():
    return cdk.App()


@pytest.fixture
def prod_app():
    return cdk.App()
