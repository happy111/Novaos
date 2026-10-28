"""CdkIamDeployRolesStack — IAM roles for the NOVOS AR platform.

Creates:
  - CohortLabelExecution:
      Trusted by: sagemaker.amazonaws.com
      Purpose: Identity the population + label Processing job (AR-18-a-2 stage 2) runs
               as — never the broad dev execution role. Reads the compiled intake and
               the rendered source SQL from the cohort-label bucket and writes the
               labelled cohort, the cohort-label report and the versioned
               cohort-datasets/ copy back to it; reads the Snowflake connection secret
               and PEM key from Secrets Manager; pulls its own image from ECR; attaches
               to the VPC so the Snowflake PrivateLink hostname resolves; reads from
               Feature Store; writes datasets to the DRA65 datasets bucket; assumes the
               DRA66 roles (post-MVP shape, unused same-account); decrypts and encrypts
               with the CMK; writes to the SageMaker Processing job log group.
      Granted:   FeatureStoreReadOnly is ATTACHED as a same-account managed policy
               (AC-8) rather than copied inline.
      Publishes: /{app_prefix}/{env}/cohort-label-execution-role-arn (consumed by
               CohortLabelStateMachine via iam:PassRole, and named by the compiled
               intake's job.role_arn via COHORT_LABEL_EXECUTION_ROLE_ARN)

  - CohortLabelStateMachine:
      Trusted by: states.amazonaws.com
      Purpose: Identity used by AWS Step Functions to orchestrate the cohort label
               pipeline (CdkCohortLabelStack). Invokes the cohort-validation Lambda,
               creates, describes and stops the population + label Processing job, and
               passes CohortLabelExecution to it. The wait loop is a native aws-sdk
               integration rather than a Lambda. Needs no S3 or KMS — the Lambda and
               the Processing job do that work.
      Publishes: /{app_prefix}/{env}/cohort-label-statemachine-role-arn (consumed by
               state_machines.cohort_label.role in environments/<env>.json)

  - ModelReadyDatasetStateMachine:
      Trusted by: states.amazonaws.com
      Purpose: Identity used by AWS Step Functions to orchestrate the
               model-ready-dataset pipeline (CdkModelReadyDatasetStack, NTU-1038).
               Invokes the mrd-validation Lambda, then creates, describes and stops
               the three `md`-kind Processing jobs (pit / spl / val) and passes
               CohortLabelExecution to each. Its own role rather than a second
               consumer of CohortLabelStateMachine: the two pipelines invoke
               different validators, so sharing would grant each the other's.
               Needs no S3, KMS or SNS — the Lambda and the Processing jobs do that
               work, including the validate stage's failure alert.
      Publishes: /{app_prefix}/{env}/mrd-statemachine-role-arn (consumed by
               state_machines.model_ready_dataset.role in environments/<env>.json)

  - CohortLabelLambda:
      Trusted by: lambda.amazonaws.com
      Purpose: execution role for the cohort-label intake compiler (stage 1). Reads the
               staged intake directory from the config bucket and writes the compiled
               intake to the cohort-label bucket. Strictly narrower than
               FeatureIngestionLambda: no feature groups, no Glue, no PassRole.
      Publishes: /{app_prefix}/{env}/lambda-cohort-label-role-arn (consumed by
               lambdas.cohort-validation.role in environments/<env>.json)

  - ModelReadyDatasetLambda:
      Trusted by: lambda.amazonaws.com
      Purpose: execution role for the model-ready-dataset intake compiler (NTU-1038
               stage 1). Its own role rather than a second consumer of
               CohortLabelLambda, which can only write the cohort-label bucket and
               cannot read an upstream compiled intake at all. Reads the staged MRD
               intake directory from the config bucket and the upstream cohort-label
               run's compiled intake, lists the cohort bucket to resolve `latest`, and
               writes [Run ID]/compile-output/ to the model-ready-dataset bucket. The
               three Processing stages run as CohortLabelExecution, so they are
               deliberately not covered here.
      Publishes: /{app_prefix}/{env}/lambda-mrd-role-arn (consumed by
               lambdas.mrd-validation.role in environments/<env>.json)

  - WorkloadPipelineInvoke:
      Trusted by: sagemaker.amazonaws.com
      Purpose: Allows SageMaker to start and describe approved workload pipelines.
               Scoped to specific pipeline ARNs — does not grant broad SageMaker access.
      Publishes: /{app_prefix}/{env}/workload-pipelineinvoke-role-arn (consumed by pipeline trigger Lambda or EventBridge rule)

  - WorkloadPipelineExecution:
      Trusted by: sagemaker.amazonaws.com
      Purpose: Identity used by SageMaker jobs within the workload pipeline (training,
               processing, AutoML). Reads/writes workload report store and model artifact
               buckets, pulls approved training images from ECR, registers model packages
               in the local registry, assumes cross-account roles in DRA66 for feature
               store reads and dataset writes, and decrypts KMS keys in both accounts.
      Publishes: /{app_prefix}/{env}/workload-pipeline-execution-role-arn (consumed by workload pipeline step definitions)

  - WorkloadFeatureStoreIngestRole:
      Trusted by: sagemaker.amazonaws.com (scoped to source account + ARN condition)
      Purpose: Identity used by SageMaker Processing jobs that ingest data into the
               Feature Store. Reads Snowflake credentials from Secrets Manager, writes
               records to SageMaker Feature Groups, writes to the S3 offline store,
               and manages the Glue catalog for the per-feature-group offline databases.
               Stage 4 also reads the compiled intake and the clipped source SQL from
               the feature-ingestion bucket, writes the ingest report back to it,
               pulls its own image from ECR, and writes to the SageMaker Processing
               job log group. A column-derived Pattern C feature resolves its real
               names from this same job's own already-fetched source DataFrame, so
               this role also needs UpdateFeatureGroup (to add those resolved
               features) and UpdateFeatureMetadata (to document them) — the same two
               calls stage 2's FeatureIngestionLambda role already has for the
               static features known at compile time.
      Publishes: /{app_prefix}/{env}/workload-featurestore-ingest-role-arn (consumed by Feature Store ingestion pipeline steps)

  - FeatureStoreReadOnly (customer-managed policy, not a role):
      Attached to: CohortLabelExecution — which is what makes the M4 PIT-join stage's
               offline-store read a policy ATTACHMENT (AC-1 / SPEC-M4 §4.4) rather
               than an inline copy. Also intended for the MLPlatform-DataScientist
               principal once AR-03-b provisions it.
      Purpose: Read-only Feature Store access covering both read paths the M2 LLD
               grants a DS (§5, §6): the online store (GetRecord / BatchGetRecord /
               DescribeFeatureGroup) and the offline store (Glue catalog read +
               Athena query + S3 read on the offline prefixes).
               Strictly narrower than WorkloadFeatureStoreIngestRole — no Snowflake
               secret read, no sagemaker:PutRecord, no Glue write. The single write
               granted is the Athena query-results prefix, which Athena requires in
               order to execute any query at all.
               NTU-1038 widened the Glue and S3 scopes: they named
               `ml_feature_store` and the SageMaker default bucket, neither of which
               this platform creates — feature-group-creater catalogs each group into
               a database named after the GROUP, in the offline-store bucket. As
               written the policy denied every offline read it existed to allow.
      Publishes: /{app_prefix}/{env}/featurestore-read-policy-arn (consumed by the DS
               profile / permission-set attachment in AR-03-b)

  - DatasetWrite (customer-managed policy, not a role):
      Attached to: CohortLabelExecution — the same-account attachment SPEC-M4 §4.4
               requires for the dataset area (AC-9). The dra66_* cross-account ARNs
               in the env JSON are the post-MVP shape and stay unwired.
      Purpose: Read/write the model-ready-dataset bucket for the three M4 MRD
               Processing jobs. Carries the reads as well as the writes because the
               dataset area is ONE bucket that every stage both reads and writes —
               the compiled intake, the Athena results, the pit-join and splits
               scratch, the validation report and the versioned dataset.
      Publishes: /{app_prefix}/{env}/dataset-write-policy-arn

  - WorkloadStudioExecution:
      Trusted by: sagemaker.amazonaws.com
      Purpose: Identity used by SageMaker Studio users/kernels to run training and
               processing jobs, track experiments via SageMaker Experiments, read/write
               studio artifact bucket, and pass an approved execution role to SageMaker
               jobs via iam:PassRole (conditioned on iam:PassedToService).
      Publishes: /{app_prefix}/{env}/workload-studioexecution-role-arn (consumed by Studio domain/user profile configuration)

  - FeatureIngestionLambda:
      Trusted by: lambda.amazonaws.com
      Purpose: shared identity for every Lambda stage of the feature-ingestion
               state machine — the intake compiler (reads the form, writes the
               compiled intake) and the feature group creater (creates, deletes
               and documents feature groups, and passes the ingest role to Feature
               Store). Stage 3 polls DescribeFeatureGroup as a native aws-sdk
               integration in the state machine, and stage 4 is a SageMaker
               Processing job running as WorkloadFeatureStoreIngestRole, so
               neither is covered here.
      Publishes: /{app_prefix}/{env}/lambda-feature-ingestion-role-arn (consumed
               by the lambdas block in environments/<env>.json)

  - FeatureOnboardingStateMachine:
      Trusted by: states.amazonaws.com
      Purpose: identity used by the feature onboarding state machine
               (CdkFeatureOnboardingStack). Invokes the two feature-ingestion Lambda
               stages, calls DescribeFeatureGroup itself for the wait loop, and
               creates, describes and stops the stage 4 ingestion Processing job,
               passing it the ingest role. Both wait loops are native aws-sdk
               integrations rather than Lambdas. Needs no S3 or KMS — the Lambdas
               and the Processing job do that work.
      Publishes: /{app_prefix}/{env}/statemachine-feature-onboarding-role-arn
               (consumed by state_machines.feature_onboarding.role in
               environments/<env>.json)

Reads from environments/*.json:
  - kms_key_arn          — DRA65 KMS key for S3/SageMaker encryption
  - kms_key_arn_dra66    — DRA66 KMS key for cross-account data access
  - dra66_dataset_write_role_arn — cross-account role in DRA66 (484907503679)
  - permissions_boundary — mandatory Novartis IAM boundary applied to all roles
"""

from typing import Any

from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from constructs import Construct

# Actions several roles need, named so a correction lands in one place.
SAGEMAKER_DESCRIBE_FEATURE_GROUP = "sagemaker:DescribeFeatureGroup"
S3_ABORT_MULTIPART_UPLOAD = "s3:AbortMultipartUpload"
S3_GET_BUCKET_LOCATION = "s3:GetBucketLocation"
LOGS_DESCRIBE_LOG_STREAMS = "logs:DescribeLogStreams"
# The condition key that pins a PassRole to the service allowed to receive it.
IAM_PASSED_TO_SERVICE = "iam:PassedToService"

# Service principals assumed by more than one role.
SAGEMAKER_SERVICE_PRINCIPAL = "sagemaker.amazonaws.com"
STATES_SERVICE_PRINCIPAL = "states.amazonaws.com"
LAMBDA_SERVICE_PRINCIPAL = "lambda.amazonaws.com"

# S3 actions needed by more than one role/policy.
S3_GET_OBJECT = "s3:GetObject"
S3_PUT_OBJECT = "s3:PutObject"
S3_LIST_BUCKET = "s3:ListBucket"
S3_PREFIX_CONDITION_KEY = "s3:prefix"

# ECR actions needed by more than one role.
ECR_GET_AUTHORIZATION_TOKEN = "ecr:GetAuthorizationToken"
ECR_BATCH_CHECK_LAYER_AVAILABILITY = "ecr:BatchCheckLayerAvailability"
ECR_BATCH_GET_IMAGE = "ecr:BatchGetImage"
ECR_GET_DOWNLOAD_URL_FOR_LAYER = "ecr:GetDownloadUrlForLayer"
ECR_DESCRIBE_IMAGES = "ecr:DescribeImages"
ECR_DESCRIBE_REPOSITORIES = "ecr:DescribeRepositories"

# EC2 ENI actions needed for VPC attachment, needed by more than one role.
EC2_DESCRIBE_NETWORK_INTERFACES = "ec2:DescribeNetworkInterfaces"
EC2_CREATE_NETWORK_INTERFACE = "ec2:CreateNetworkInterface"
EC2_CREATE_NETWORK_INTERFACE_PERMISSION = "ec2:CreateNetworkInterfacePermission"
EC2_DELETE_NETWORK_INTERFACE = "ec2:DeleteNetworkInterface"
EC2_DELETE_NETWORK_INTERFACE_PERMISSION = "ec2:DeleteNetworkInterfacePermission"
EC2_DESCRIBE_DHCP_OPTIONS = "ec2:DescribeDhcpOptions"
EC2_DESCRIBE_SECURITY_GROUPS = "ec2:DescribeSecurityGroups"
EC2_DESCRIBE_SUBNETS = "ec2:DescribeSubnets"
EC2_DESCRIBE_VPCS = "ec2:DescribeVpcs"

# STS / KMS actions needed by more than one role.
STS_ASSUME_ROLE = "sts:AssumeRole"
KMS_DECRYPT = "kms:Decrypt"
KMS_ENCRYPT = "kms:Encrypt"
KMS_GENERATE_DATA_KEY = "kms:GenerateDataKey"
KMS_DESCRIBE_KEY = "kms:DescribeKey"

# CloudWatch Logs actions needed by more than one role.
LOGS_CREATE_LOG_GROUP = "logs:CreateLogGroup"
LOGS_CREATE_LOG_STREAM = "logs:CreateLogStream"
LOGS_PUT_LOG_EVENTS = "logs:PutLogEvents"
LOGS_CREATE_LOG_DELIVERY = "logs:CreateLogDelivery"
LOGS_DELETE_LOG_DELIVERY = "logs:DeleteLogDelivery"
LOGS_DESCRIBE_LOG_GROUPS = "logs:DescribeLogGroups"
LOGS_DESCRIBE_RESOURCE_POLICIES = "logs:DescribeResourcePolicies"
LOGS_GET_LOG_DELIVERY = "logs:GetLogDelivery"
LOGS_LIST_LOG_DELIVERIES = "logs:ListLogDeliveries"
LOGS_PUT_RESOURCE_POLICY = "logs:PutResourcePolicy"
LOGS_UPDATE_LOG_DELIVERY = "logs:UpdateLogDelivery"
LOGS_PUT_RETENTION_POLICY = "logs:PutRetentionPolicy"
LOGS_DELETE_RETENTION_POLICY = "logs:DeleteRetentionPolicy"

# Lambda / SageMaker / IAM actions needed by more than one role.
LAMBDA_INVOKE_FUNCTION = "lambda:InvokeFunction"
SAGEMAKER_CREATE_PROCESSING_JOB = "sagemaker:CreateProcessingJob"
SAGEMAKER_DESCRIBE_PROCESSING_JOB = "sagemaker:DescribeProcessingJob"
SAGEMAKER_STOP_PROCESSING_JOB = "sagemaker:StopProcessingJob"
SAGEMAKER_ADD_TAGS = "sagemaker:AddTags"
SAGEMAKER_LIST_TAGS = "sagemaker:ListTags"
ROLE_DELEGATION_ACTION = "iam:PassRole"

# Glue actions needed by more than one role.
GLUE_GET_DATABASE = "glue:GetDatabase"
GLUE_GET_TABLE = "glue:GetTable"


class CdkIamDeployRolesStack(Stack):
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
        account = var_config["account"]
        region = var_config["region"]
        account_role_prefix = var_config.get("account_role_prefix", "")
        policy_prefix = var_config.get("policy_prefix", "")
        # ── KMS key ARN — read from config for IAM policy resource scoping ───
        kms_key_arn = var_config["kms_key_arn"]

        permissions_boundary = iam.ManagedPolicy.from_managed_policy_arn(
            self, "PermBoundary", var_config["permissions_boundary"]
        )

        kms_key_arn_dra66 = var_config["kms_key_arn_dra66"]
        dra66_dataset_write_role_arn = var_config["dra66_dataset_write_role_arn"]
        dra66_featurestore_read_role_arn = var_config[
            "dra66_featurestore_read_role_arn"
        ]
        datasets_bucket = f"novartis-{account}-{environment}-{app_prefix}-datasets"

        # The bucket names cdk_s3_buckets.py actually composes — the region is
        # part of the name. Used by the feature-ingestion stage roles below.
        def bucket_name(suffix: str) -> str:
            return f"novartis-{account}-{environment}-{region}-{app_prefix}-{suffix}"

        feature_ingestion_bucket = bucket_name("feature-ingestion")
        ds_datasets_bucket = bucket_name("datasets")
        # Model-training artifact bucket: the model-training Lambdas read the M4
        # datasets from here and the pipeline writes its outputs here (AR_ARTIFACT_BUCKET).
        model_training_bucket = bucket_name("model-training")

        # Where the cohort-label pipeline reads its compiled intake and rendered SQL
        # and writes the labelled cohort
        cohort_label_bucket = bucket_name("cohort-label")
        # The model-ready-dataset pipeline's own bucket
        mrd_datasets_bucket = bucket_name("mrd-datasets")
        # Where the platform's feature groups put their offline store
        offline_store_bucket = bucket_name("offline-store")
        # The feature-intake directory a team uploads before a run lands here. Not
        # created by cdk_s3_buckets.py on this branch — it is referenced, not owned.
        config_bucket = bucket_name("config")

        # Feature groups this platform creates are named ar-<wl>-<env>-fgr-<name>
        # by the intake compiler, so the grants below cannot be enumerated but can
        # be scoped to this environment's groups.
        feature_group_arn = f"arn:aws:sagemaker:{region}:{account}:feature-group/ar-*-{environment}-fgr-*"

        # The Processing jobs each state machine launches, scoped by job KIND: `fi` for
        # feature ingestion, `cl` for cohort-label, `md` for the three model-ready-dataset
        # stages
        ingest_processing_job_arn = (
            f"arn:aws:sagemaker:{region}:{account}:processing-job/fi-*"
        )
        cohort_label_processing_job_arn = (
            f"arn:aws:sagemaker:{region}:{account}:processing-job/cl-*"
        )
        mrd_processing_job_arn = (
            f"arn:aws:sagemaker:{region}:{account}:processing-job/md-*"
        )

        # qa.json and prod.json do not declare ecr_repo_name yet; the default keeps
        # them synthesizable and matches the repo the existing roles reference.
        ecr_repo_name = var_config.get("ecr_repo_name", "f1ai/applications/pwsa0002037")

        # ── CohortLabelExecution Role ─────────────────────────────────
        execution_role = iam.Role(
            self,
            "CohortLabelExecutionRole",
            role_name=f"{account_role_prefix}{app_prefix}-CohortLabelExecution",
            assumed_by=iam.ServicePrincipal(SAGEMAKER_SERVICE_PRINCIPAL),
            description="SageMaker Processing job execution role for NOVOS AR cohort label pipeline",
            permissions_boundary=permissions_boundary,
        )

        execution_policy = iam.Policy(
            self,
            "CohortLabelExecutionPolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-execution-policy",
            statements=[
                iam.PolicyStatement(
                    sid="FeatureStoreAccess",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:GetRecord",
                        "sagemaker:BatchGetRecord",
                        SAGEMAKER_DESCRIBE_FEATURE_GROUP,
                        "sagemaker:ListFeatureGroups",
                    ],
                    resources=[f"arn:aws:sagemaker:{region}:{account}:feature-group/*"],
                ),
                iam.PolicyStatement(
                    sid="FeatureStoreList",
                    effect=iam.Effect.ALLOW,
                    actions=["sagemaker:ListFeatureGroups"],
                    resources=["*"],  # NOSONAR
                ),
                iam.PolicyStatement(
                    sid="S3ReadWriteDatasetLocal",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        S3_GET_OBJECT,
                        S3_PUT_OBJECT,
                        S3_LIST_BUCKET,
                    ],
                    resources=[
                        f"arn:aws:s3:::{ds_datasets_bucket}",
                        f"arn:aws:s3:::{ds_datasets_bucket}/*",
                    ],
                ),
                # ── What the population + label Processing job itself does ──
                iam.PolicyStatement(
                    sid="ReadCompiledIntakeAndSourceSql",
                    effect=iam.Effect.ALLOW,
                    # [Run ID]/compile-output/{compiled_intake.json,
                    # population_source.sql}, both written by the validator Lambda.
                    actions=[S3_GET_OBJECT],
                    resources=[f"arn:aws:s3:::{cohort_label_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="ListUpstreamCohortDataset",
                    effect=iam.Effect.ALLOW,
                    # The model-ready-dataset pit-join stage reads the upstream cohort
                    # from a PREFIX (cohort-datasets/<model>/<version>/)
                    actions=[S3_LIST_BUCKET, S3_GET_BUCKET_LOCATION],
                    resources=[f"arn:aws:s3:::{cohort_label_bucket}"],
                ),
                iam.PolicyStatement(
                    sid="WriteLabelledCohortAndReport",
                    effect=iam.Effect.ALLOW,
                    # [Run ID]/population-label/ for the run's own output, and
                    # cohort-datasets/ for the versioned destination storage in S3
                    actions=[S3_PUT_OBJECT],
                    resources=[f"arn:aws:s3:::{cohort_label_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="ReadSnowflakeSecret",
                    effect=iam.Effect.ALLOW,
                    # The connection secret and the PEM private key. Secrets Manager
                    # appends a 6-character suffix to every secret ARN, which is why
                    # the names cannot be matched exactly.
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        f"arn:aws:secretsmanager:{region}:{account}:secret:f1ai-pwsa0002037-??????",
                        f"arn:aws:secretsmanager:{region}:{account}:secret:f1ai-pwsa0002037-key-??????",
                    ],
                ),
                iam.PolicyStatement(
                    sid="EcrAuthToken",
                    effect=iam.Effect.ALLOW,
                    # The job cannot pull its own image without this, and
                    # GetAuthorizationToken takes no resource.
                    actions=[ECR_GET_AUTHORIZATION_TOKEN],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="EcrImageAccess",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                        ECR_BATCH_GET_IMAGE,
                        ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                    ],
                    resources=[
                        f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="SnowflakePrivateLinkEndpointAccess",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "ec2:DescribeVpcEndpoints",
                        EC2_DESCRIBE_NETWORK_INTERFACES,
                    ],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="VpcNetworkInterfaces",
                    effect=iam.Effect.ALLOW,
                    # The job runs in private subnets so the PrivateLink hostname
                    # resolves; SageMaker attaches the ENI as this role.
                    actions=[
                        EC2_CREATE_NETWORK_INTERFACE,
                        EC2_CREATE_NETWORK_INTERFACE_PERMISSION,
                        EC2_DELETE_NETWORK_INTERFACE,
                        EC2_DELETE_NETWORK_INTERFACE_PERMISSION,
                        EC2_DESCRIBE_DHCP_OPTIONS,
                        EC2_DESCRIBE_SECURITY_GROUPS,
                        EC2_DESCRIBE_SUBNETS,
                        EC2_DESCRIBE_VPCS,
                    ],
                    # AWS does not support resource-level restrictions for these
                    # EC2 network interface actions.
                    resources=["*"],  # NOSONAR
                ),
                iam.PolicyStatement(
                    sid="AssumeDatasetWriteCrossAccount",
                    effect=iam.Effect.ALLOW,
                    actions=[STS_ASSUME_ROLE],
                    resources=[dra66_dataset_write_role_arn],
                ),
                iam.PolicyStatement(
                    sid="AssumeFeatureStoreReadCrossAccount",
                    effect=iam.Effect.ALLOW,
                    actions=[STS_ASSUME_ROLE],
                    resources=[dra66_featurestore_read_role_arn],
                ),
                iam.PolicyStatement(
                    sid="KMSAccessDRA65",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        KMS_DECRYPT,
                        # The job writes the cohort and the report with SSE-KMS
                        KMS_ENCRYPT,
                        KMS_GENERATE_DATA_KEY,
                        KMS_DESCRIBE_KEY,
                    ],
                    resources=[kms_key_arn],
                ),
                iam.PolicyStatement(
                    sid="KMSAccessDRA66",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        KMS_DECRYPT,
                        KMS_GENERATE_DATA_KEY,
                        KMS_DESCRIBE_KEY,
                    ],
                    resources=[kms_key_arn_dra66],
                ),
                iam.PolicyStatement(
                    sid="CloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_GROUP,
                        LOGS_CREATE_LOG_STREAM,
                        LOGS_PUT_LOG_EVENTS,
                        LOGS_DESCRIBE_LOG_STREAMS,
                    ],
                    resources=[
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/sagemaker/ProcessingJobs:*"
                    ],
                ),
                iam.PolicyStatement(
                    sid="SNSPublishAlerts",
                    effect=iam.Effect.ALLOW,
                    actions=["sns:Publish"],
                    resources=[
                        f"arn:aws:sns:{region}:{account}:{app_prefix}-ds-alert-{environment}"
                    ],
                ),
            ],
        )
        execution_role.attach_inline_policy(execution_policy)

        ssm.StringParameter(
            self,
            "CohortLabelExecutionRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/cohort-label-execution-role-arn",
            string_value=execution_role.role_arn,
            description="CohortLabelExecution role ARN",
        )

        # ── CohortLabelStateMachine Role ──────────────────────────────
        statemachine_role = iam.Role(
            self,
            "CohortLabelStateMachineRole",
            role_name=f"{account_role_prefix}{app_prefix}-CohortLabelStateMachine",
            assumed_by=iam.ServicePrincipal(STATES_SERVICE_PRINCIPAL),
            description="Step Functions execution role for NOVOS AR cohort label pipeline",
            permissions_boundary=permissions_boundary,
        )

        statemachine_policy = iam.Policy(
            self,
            "CohortLabelStateMachinePolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-statemachine-policy",
            statements=[
                iam.PolicyStatement(
                    sid="InvokeCohortValidationLambda",
                    effect=iam.Effect.ALLOW,
                    actions=[LAMBDA_INVOKE_FUNCTION],
                    resources=[
                        f"arn:aws:lambda:{region}:{account}:function:"
                        f"{app_prefix}-cohort-validation-{environment}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="StartSageMakerJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        SAGEMAKER_CREATE_PROCESSING_JOB,
                        SAGEMAKER_DESCRIBE_PROCESSING_JOB,
                        SAGEMAKER_STOP_PROCESSING_JOB,
                        SAGEMAKER_ADD_TAGS,
                    ],
                    resources=[cohort_label_processing_job_arn],
                ),
                iam.PolicyStatement(
                    sid="PassExecutionRole",
                    effect=iam.Effect.ALLOW,
                    actions=[ROLE_DELEGATION_ACTION],
                    resources=[execution_role.role_arn],
                    conditions={
                        "StringEquals": {
                            IAM_PASSED_TO_SERVICE: SAGEMAKER_SERVICE_PRINCIPAL
                        }
                    },
                ),
                iam.PolicyStatement(
                    sid="StepFunctionsCloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_DELIVERY,
                        LOGS_DELETE_LOG_DELIVERY,
                        LOGS_DESCRIBE_LOG_GROUPS,
                        LOGS_DESCRIBE_RESOURCE_POLICIES,
                        LOGS_GET_LOG_DELIVERY,
                        LOGS_LIST_LOG_DELIVERIES,
                        LOGS_PUT_RESOURCE_POLICY,
                        LOGS_UPDATE_LOG_DELIVERY,
                    ],
                    resources=["*"],  # NOSONAR
                ),
                iam.PolicyStatement(
                    sid="SNSPublishValidationFailure",
                    effect=iam.Effect.ALLOW,
                    actions=["sns:Publish"],
                    resources=[
                        f"arn:aws:sns:{region}:{account}:{app_prefix}-ds-alert-{environment}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="XRayTracing",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "xray:GetSamplingRules",
                        "xray:GetSamplingTargets",
                        "xray:PutTelemetryRecords",
                        "xray:PutTraceSegments",
                    ],
                    resources=["*"],
                ),
            ],
        )
        statemachine_role.attach_inline_policy(statemachine_policy)

        ssm.StringParameter(
            self,
            "CohortLabelStateMachineRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/cohort-label-statemachine-role-arn",
            string_value=statemachine_role.role_arn,
            description="CohortLabelStateMachine role ARN",
        )

        # ── ModelReadyDatasetStateMachine Role ────────────────────────
        # NTU-1038. Its own role rather
        mrd_statemachine_role = iam.Role(
            self,
            "ModelReadyDatasetStateMachineRole",
            role_name=f"{account_role_prefix}{app_prefix}-ModelReadyDatasetStateMachine",
            assumed_by=iam.ServicePrincipal(STATES_SERVICE_PRINCIPAL),
            description=(
                "Step Functions execution role for NOVOS AR model-ready-dataset "
                "pipeline"
            ),
            permissions_boundary=permissions_boundary,
        )

        mrd_statemachine_policy = iam.Policy(
            self,
            "ModelReadyDatasetStateMachinePolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-mrd-statemachine-policy",
            statements=[
                iam.PolicyStatement(
                    sid="InvokeMrdValidationLambda",
                    effect=iam.Effect.ALLOW,
                    actions=[LAMBDA_INVOKE_FUNCTION],
                    resources=[
                        f"arn:aws:lambda:{region}:{account}:function:"
                        f"{app_prefix}-mrd-validation-{environment}"
                    ],
                ),
                iam.PolicyStatement(
                    # All three stages — pit, spl and val — are `md`-kind jobs and so
                    # fall inside the same wildcard.
                    sid="StartSageMakerJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        SAGEMAKER_CREATE_PROCESSING_JOB,
                        SAGEMAKER_DESCRIBE_PROCESSING_JOB,
                        SAGEMAKER_STOP_PROCESSING_JOB,
                        SAGEMAKER_ADD_TAGS,
                    ],
                    resources=[mrd_processing_job_arn],
                ),
                iam.PolicyStatement(
                    sid="PassExecutionRole",
                    effect=iam.Effect.ALLOW,
                    resources=[execution_role.role_arn],
                    actions=[ROLE_DELEGATION_ACTION],
                    conditions={
                        "StringEquals": {
                            IAM_PASSED_TO_SERVICE: SAGEMAKER_SERVICE_PRINCIPAL
                        }
                    },
                ),
                iam.PolicyStatement(
                    # V28: a Standard workflow logs nothing without an explicit
                    # LoggingConfiguration, and the delivery needs these.
                    sid="StepFunctionsCloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_DELIVERY,
                        LOGS_DELETE_LOG_DELIVERY,
                        LOGS_DESCRIBE_LOG_GROUPS,
                        LOGS_DESCRIBE_RESOURCE_POLICIES,
                        LOGS_GET_LOG_DELIVERY,
                        LOGS_LIST_LOG_DELIVERIES,
                        LOGS_PUT_RESOURCE_POLICY,
                        LOGS_UPDATE_LOG_DELIVERY,
                    ],
                    # AWS does not support resource-level restrictions for the vended-
                    # logs delivery actions
                    resources=["*"],  # NOSONAR
                ),
                iam.PolicyStatement(
                    sid="XRayTracing",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "xray:GetSamplingRules",
                        "xray:GetSamplingTargets",
                        "xray:PutTelemetryRecords",
                        "xray:PutTraceSegments",
                    ],
                    resources=["*"],
                ),
            ],
        )
        mrd_statemachine_role.attach_inline_policy(mrd_statemachine_policy)

        ssm.StringParameter(
            self,
            "ModelReadyDatasetStateMachineRoleArn",
            parameter_name=(f"/{app_prefix}/{environment}/mrd-statemachine-role-arn"),
            string_value=mrd_statemachine_role.role_arn,
            description="ModelReadyDatasetStateMachine role ARN",
        )
        # ── WorkloadPipelineInvoke Role ───────────────────────────────
        # Trusted by sagemaker.amazonaws.com
        # Purpose: Allows SageMaker to start and describe approved workload pipelines
        pipeline_invoke_role = iam.Role(
            self,
            "WorkloadPipelineInvokeRole",
            role_name=f"{account_role_prefix}{app_prefix}-workload-pipelineinvoke",
            assumed_by=iam.ServicePrincipal(SAGEMAKER_SERVICE_PRINCIPAL),
            description="Allows SageMaker to invoke approved workload pipelines",
            permissions_boundary=permissions_boundary,
        )

        pipeline_invoke_policy = iam.Policy(
            self,
            "WorkloadPipelineInvokePolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-workload-pipelineinvoke-policy",
            statements=[
                iam.PolicyStatement(
                    sid="AllowStartApprovedSageMakerPipelines",
                    effect=iam.Effect.ALLOW,
                    actions=["sagemaker:StartPipelineExecution"],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:pipeline/dummy-workload-pipeline-{environment}",
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowDescribeApprovedPipelineExecutions",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:DescribePipeline",
                        "sagemaker:DescribePipelineExecution",
                        "sagemaker:ListPipelineExecutionSteps",
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:pipeline/dummy-workload-pipeline-{environment}",
                    ],
                ),
            ],
        )
        pipeline_invoke_role.attach_inline_policy(pipeline_invoke_policy)

        ssm.StringParameter(
            self,
            "WorkloadPipelineInvokeRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/workload-pipelineinvoke-role-arn",
            string_value=pipeline_invoke_role.role_arn,
            description="WorkloadPipelineInvoke role ARN",
        )

        # ── WorkloadPipelineExecution Role ────────────────────────────
        # Trusted by sagemaker.amazonaws.com
        # Purpose: Identity used by SageMaker training/processing/AutoML jobs
        #          within the workload pipeline. Accesses workload report store,
        #          model artifact buckets, ECR images, model registry, cross-account
        #          feature store and dataset roles, KMS keys in DRA65 and DRA66.
        workload_execution_role = iam.Role(
            self,
            "WorkloadPipelineExecutionRole",
            role_name=f"{account_role_prefix}{app_prefix}-workload-pipelineexecution",
            assumed_by=iam.ServicePrincipal(SAGEMAKER_SERVICE_PRINCIPAL),
            description="SageMaker job execution role for NOVOS AR workload pipeline",
            permissions_boundary=permissions_boundary,
        )

        workload_execution_policy = iam.Policy(
            self,
            "WorkloadPipelineExecutionPolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-workload-pipelineexecution-policy",
            statements=[
                iam.PolicyStatement(
                    sid="AllowAssumeCentralFeatureStoreRead",
                    effect=iam.Effect.ALLOW,
                    actions=[STS_ASSUME_ROLE],
                    resources=[dra66_featurestore_read_role_arn],
                ),
                iam.PolicyStatement(
                    sid="AllowAssumeCentralDatasetWrite",
                    effect=iam.Effect.ALLOW,
                    actions=[STS_ASSUME_ROLE],
                    resources=[dra66_dataset_write_role_arn],
                ),
                iam.PolicyStatement(
                    sid="AllowReadCentralDataset",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_GET_OBJECT],
                    resources=[f"arn:aws:s3:::{datasets_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="AllowListCentralDatasetBucket",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_LIST_BUCKET],
                    resources=[f"arn:aws:s3:::{datasets_bucket}"],
                ),
                iam.PolicyStatement(
                    sid="AllowWriteWorkloadReports",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_GET_OBJECT, S3_PUT_OBJECT, S3_ABORT_MULTIPART_UPLOAD],
                    resources=[
                        f"arn:aws:s3:::novartis-{account}-{environment}-{app_prefix}-wl-report-store/*"
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowListWorkloadReportPrefix",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_LIST_BUCKET],
                    resources=[
                        f"arn:aws:s3:::novartis-{account}-{environment}-{app_prefix}-wl-report-store"
                    ],
                    conditions={"StringLike": {S3_PREFIX_CONDITION_KEY: ["reports/*"]}},
                ),
                iam.PolicyStatement(
                    sid="AllowReadWriteModelArtifacts",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_GET_OBJECT, S3_PUT_OBJECT, S3_ABORT_MULTIPART_UPLOAD],
                    resources=[f"arn:aws:s3:::sagemaker-{region}-{account}/*"],
                ),
                iam.PolicyStatement(
                    sid="AllowListModelArtifactPrefix",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_LIST_BUCKET],
                    resources=[f"arn:aws:s3:::sagemaker-{region}-{account}"],
                    conditions={
                        "StringLike": {S3_PREFIX_CONDITION_KEY: ["training/*"]}
                    },
                ),
                # The M5 pipeline reads the M4 dataset from and writes prep/eval/model
                # artifacts to the dedicated model-training bucket (AR_ARTIFACT_BUCKET).
                # The Prep/Train/Evaluate jobs run as THIS role (passed by the launch
                # Lambda), so the grant must live here — the Lambda's own model-training
                # grant does not extend to the jobs.
                iam.PolicyStatement(
                    sid="AllowReadWriteModelTrainingBucket",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        S3_GET_OBJECT,
                        S3_PUT_OBJECT,
                        S3_LIST_BUCKET,
                        S3_ABORT_MULTIPART_UPLOAD,
                    ],
                    resources=[
                        f"arn:aws:s3:::{model_training_bucket}",
                        f"arn:aws:s3:::{model_training_bucket}/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowReadMrdDataset",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_GET_OBJECT, S3_LIST_BUCKET],
                    resources=[
                        f"arn:aws:s3:::{mrd_datasets_bucket}",
                        f"arn:aws:s3:::{mrd_datasets_bucket}/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowEcrAuthToken",
                    effect=iam.Effect.ALLOW,
                    actions=[ECR_GET_AUTHORIZATION_TOKEN],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="AllowEcrPullForApprovedTrainingImage",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                        ECR_BATCH_GET_IMAGE,
                        ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                        ECR_DESCRIBE_IMAGES,
                        ECR_DESCRIBE_REPOSITORIES,
                    ],
                    # The M5 prep/train/evaluate images live in the app's shared ECR
                    # repo (config.resolve_image_uri composes <acct>.dkr.ecr.<region>/
                    # <image_repo>:<tag>, and image_repo = ecr_repo_name). Mirrors the
                    # Lambda role's EcrImageAccess grant so both scope to the same repo.
                    resources=[
                        f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowEcrPullForModelImages",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                        ECR_BATCH_GET_IMAGE,
                        ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                        ECR_DESCRIBE_IMAGES,
                        ECR_DESCRIBE_REPOSITORIES,
                    ],
                    resources=[
                        f"arn:aws:ecr:{region}:{account}:repository/sample-models-*",
                        f"arn:aws:ecr:{region}:{account}:repository/*-preprocessing",
                        f"arn:aws:ecr:{region}:{account}:repository/*-training",
                        f"arn:aws:ecr:{region}:{account}:repository/*-evaluation",
                        # naming.step_image_ref()'s per-model/per-compute scheme:
                        # novos-ar-base-<cpu|gpu>-job-<stage>-<model-slug>.
                        f"arn:aws:ecr:{region}:{account}:repository/novos-ar-base-*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowEcrPullForClarifyImage",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                        ECR_BATCH_GET_IMAGE,
                        ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                        ECR_DESCRIBE_IMAGES,
                        ECR_DESCRIBE_REPOSITORIES,
                    ],
                    resources=[
                        "arn:aws:ecr:us-east-1:205585389593:repository/"
                        "sagemaker-clarify-processing"
                    ],
                ),
                # SageMaker Pipelines auto-generates the step job names
                # (pipelines-<exec-id>-<step>-<hash>), so they cannot be pinned to a
                # prefix — the grant is scoped to the job TYPE, account + region.
                iam.PolicyStatement(
                    sid="AllowCreateAndManageTrainingJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreateTrainingJob",
                        "sagemaker:DescribeTrainingJob",
                        "sagemaker:StopTrainingJob",
                    ],
                    resources=[f"arn:aws:sagemaker:{region}:{account}:training-job/*"],
                ),
                iam.PolicyStatement(
                    sid="AllowCreateAndManageProcessingJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        SAGEMAKER_CREATE_PROCESSING_JOB,
                        SAGEMAKER_DESCRIBE_PROCESSING_JOB,
                        SAGEMAKER_STOP_PROCESSING_JOB,
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:processing-job/*"
                    ],
                ),
                # SageMaker Pipelines tags every job/model-package it creates with the
                # pipeline + execution metadata, so Create*Job / CreateModelPackage with
                # those tags is denied unless the role can also AddTags on the created
                # resource. Scoped to the resource types this role creates.
                iam.PolicyStatement(
                    sid="AllowTagSageMakerResources",
                    effect=iam.Effect.ALLOW,
                    actions=[SAGEMAKER_ADD_TAGS],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:training-job/*",
                        f"arn:aws:sagemaker:{region}:{account}:processing-job/*",
                        f"arn:aws:sagemaker:{region}:{account}:automl-job/*",
                        f"arn:aws:sagemaker:{region}:{account}:model-package-group/*",
                        f"arn:aws:sagemaker:{region}:{account}:model-package/*/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowAutopilotIfEnabled",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreateAutoMLJobV2",
                        "sagemaker:DescribeAutoMLJobV2",
                        "sagemaker:StopAutoMLJob",
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:automl-job/PLACEHOLDER-job-prefix-*"
                    ],
                ),
                # The RegisterModel step creates the model package into a PER-MODEL
                # group named naming.model_package_group() = _slug(model_name) (no
                # app/env prefix, unless AR_MODEL_PACKAGE_GROUP overrides it). The group
                # name isn't known ahead of time and there is one per model, so the
                # grant can't enumerate them — scope to all groups/packages in-account.
                iam.PolicyStatement(
                    sid="AllowCreateModelPackageInWorkloadLocalRegistry",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreateModelPackage",
                        "sagemaker:DescribeModelPackage",
                        "sagemaker:UpdateModelPackage",
                        # The RegisterModel step creates the per-model group on first
                        # run if it does not exist, so the job role (not just the launch
                        # Lambda) needs the group create/tag actions too.
                        "sagemaker:CreateModelPackageGroup",
                        "sagemaker:DescribeModelPackageGroup",
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:model-package-group/*",
                        f"arn:aws:sagemaker:{region}:{account}:model-package/*/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowKmsForWorkloadAndCentralArtifacts",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        KMS_DECRYPT,
                        KMS_ENCRYPT,
                        KMS_GENERATE_DATA_KEY,
                        KMS_DESCRIBE_KEY,
                    ],
                    resources=[kms_key_arn, kms_key_arn_dra66],
                ),
                iam.PolicyStatement(
                    sid="AllowCloudWatchLogsForSageMakerJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_STREAM,
                        LOGS_PUT_LOG_EVENTS,
                        LOGS_DESCRIBE_LOG_STREAMS,
                    ],
                    resources=[
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/sagemaker/*"
                    ],
                ),
                # SageMaker Pipelines assumes THIS role to launch each step's job and
                # passes the job execution role (also this role) via PassRole. Without
                # it, CreateProcessingJob/CreateTrainingJob is denied. The ARN is built
                # as a string (not role.role_arn) to avoid a self-referential circular
                # dependency in the inline policy; the condition pins it to SageMaker.
                iam.PolicyStatement(
                    sid="AllowPassSelfToSageMakerJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[ROLE_DELEGATION_ACTION],
                    resources=[
                        f"arn:aws:iam::{account}:role/{account_role_prefix}{app_prefix}-workload-pipelineexecution"
                    ],
                    conditions={
                        "StringEquals": {
                            IAM_PASSED_TO_SERVICE: SAGEMAKER_SERVICE_PRINCIPAL
                        }
                    },
                ),
                # Every Processor/Estimator this role runs as now carries a VPC
                # network config (src/pipelines/model_training.py), matching the
                # sibling CohortLabelExecution role's own VpcNetworkInterfaces grant —
                # without it CreateProcessingJob/CreateTrainingJob is denied at the
                # network layer, confirmed against a real execution.
                iam.PolicyStatement(
                    sid="VpcNetworkInterfaces",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        EC2_CREATE_NETWORK_INTERFACE,
                        EC2_CREATE_NETWORK_INTERFACE_PERMISSION,
                        EC2_DELETE_NETWORK_INTERFACE,
                        EC2_DELETE_NETWORK_INTERFACE_PERMISSION,
                        EC2_DESCRIBE_DHCP_OPTIONS,
                        EC2_DESCRIBE_NETWORK_INTERFACES,
                        EC2_DESCRIBE_SECURITY_GROUPS,
                        EC2_DESCRIBE_SUBNETS,
                        EC2_DESCRIBE_VPCS,
                    ],
                    # AWS does not support resource-level restrictions for these
                    # EC2 network interface actions.
                    resources=["*"],  # NOSONAR
                ),
            ],
        )
        workload_execution_role.attach_inline_policy(workload_execution_policy)

        ssm.StringParameter(
            self,
            "WorkloadPipelineExecutionRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/workload-pipeline-execution-role-arn",
            string_value=workload_execution_role.role_arn,
            description="WorkloadPipelineExecution role ARN",
        )

        # ── WorkloadFeatureStoreIngestRole ────────────────────────────
        # Trusted by sagemaker.amazonaws.com (scoped to source account + ARN)
        # Purpose: Identity used by SageMaker Processing jobs that ingest data
        #          into the Feature Store. Reads Snowflake credentials from Secrets
        #          Manager, writes records to SageMaker Feature Groups, writes to
        #          the S3 offline store, and manages the Glue catalog for the
        #          per-feature-group offline databases.
        featurestore_ingest_role = iam.Role(
            self,
            "WorkloadFeatureStoreIngestRole",
            role_name=f"{account_role_prefix}{app_prefix}-workload-FeatureStore-IngestRole",
            assumed_by=iam.ServicePrincipal(
                SAGEMAKER_SERVICE_PRINCIPAL,
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:sagemaker:{region}:{account}:*"
                    },
                },
            ),
            description="SageMaker Processing job role for NOVOS AR Feature Store ingestion",
            permissions_boundary=permissions_boundary,
        )

        featurestore_ingest_policy = iam.Policy(
            self,
            "WorkloadFeatureStoreIngestPolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-workload-featurestore-ingest-policy",
            statements=[
                iam.PolicyStatement(
                    sid="ReadSnowflakeSecret",
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        f"arn:aws:secretsmanager:{region}:{account}:secret:f1ai-pwsa0002037-??????",
                        f"arn:aws:secretsmanager:{region}:{account}:secret:f1ai-pwsa0002037-key-??????",
                    ],
                ),
                iam.PolicyStatement(
                    sid="KMSEncryptDecrypt",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        KMS_DECRYPT,
                        KMS_DESCRIBE_KEY,
                        KMS_ENCRYPT,
                        KMS_GENERATE_DATA_KEY,
                    ],
                    resources=[kms_key_arn, kms_key_arn_dra66],
                ),
                iam.PolicyStatement(
                    sid="SageMakerFeatureStorePutRecord",
                    effect=iam.Effect.ALLOW,
                    actions=["sagemaker:PutRecord", "sagemaker:BatchWriteRecord"],
                    resources=[f"arn:aws:sagemaker:{region}:{account}:feature-group/*"],
                ),
                iam.PolicyStatement(
                    sid="SageMakerFeatureStoreColumnDerivedExpansion",
                    effect=iam.Effect.ALLOW,
                    # A column-derived Pattern C feature (intake_schema
                    # .is_column_derived_expansion_var) has no concrete name until
                    # the ingest job resolves it against its own already-fetched
                    # source DataFrame — feature_group.add_features then calls
                    # UpdateFeatureGroup --feature-additions, batched at the API's
                    # 100-item limit, and apply_metadata calls UpdateFeatureMetadata
                    # for each resolved feature so its description reaches the
                    # store, exactly the same as stage 2 already does for the
                    # static features this job never had to add itself.
                    # DescribeFeatureGroup is in the SAME grant, not left to the
                    # DescribeFeatureGroupWaitLoop grant below (that one belongs to
                    # FeatureOnboardingStateMachine, a different role): add_features'
                    # own _wait_for_update polls DescribeFeatureGroup after every
                    # UpdateFeatureGroup call to confirm LastUpdateStatus settled
                    # before the next batch, so this role needs it too — confirmed
                    # against real AWS (AccessDeniedException on DescribeFeatureGroup
                    # from inside that wait loop, one call after UpdateFeatureGroup
                    # itself finally succeeded).
                    actions=[
                        "sagemaker:UpdateFeatureGroup",
                        "sagemaker:UpdateFeatureMetadata",
                        SAGEMAKER_DESCRIBE_FEATURE_GROUP,
                    ],
                    resources=[f"arn:aws:sagemaker:{region}:{account}:feature-group/*"],
                ),
                iam.PolicyStatement(
                    sid="S3OfflineStoreWrite",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        S3_GET_OBJECT,
                        S3_PUT_OBJECT,
                        "s3:PutObjectAcl",
                        S3_ABORT_MULTIPART_UPLOAD,
                    ],
                    resources=[
                        f"arn:aws:s3:::{bucket_name('offline-store')}/*",
                        # The develop-branch sample writes its offline store here.
                        f"arn:aws:s3:::sagemaker-{region}-{account}/feature-store/*",
                        f"arn:aws:s3:::sagemaker-{region}-{account}/01-feature-store/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="S3OfflineStoreBucketAccess",
                    effect=iam.Effect.ALLOW,
                    # CreateFeatureGroup validates the offline S3Uri by calling
                    # GetBucketAcl as this role. Without it the create is rejected
                    # with "Invalid S3Uri provided" before the group ever exists.
                    actions=[
                        "s3:GetBucketAcl",
                        S3_GET_BUCKET_LOCATION,
                        S3_LIST_BUCKET,
                    ],
                    resources=[
                        f"arn:aws:s3:::{bucket_name('offline-store')}",
                        f"arn:aws:s3:::sagemaker-{region}-{account}",
                    ],
                ),
                iam.PolicyStatement(
                    sid="GlueCatalogWrite",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "glue:CreateDatabase",
                        "glue:CreateTable",
                        GLUE_GET_DATABASE,
                        GLUE_GET_TABLE,
                        "glue:UpdateTable",
                    ],
                    # Each feature group catalogs into its own database, named
                    # after the group by the intake compiler, so the database name
                    # is not known ahead of the run and cannot be enumerated here.
                    resources=[
                        f"arn:aws:glue:{region}:{account}:catalog",
                        f"arn:aws:glue:{region}:{account}:database/*",
                        f"arn:aws:glue:{region}:{account}:table/*/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="SnowflakePrivateLinkEndpointAccess",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "ec2:DescribeVpcEndpoints",
                        EC2_DESCRIBE_NETWORK_INTERFACES,
                    ],
                    resources=["*"],
                ),
                # ── Stage 4: what the ingestion Processing job itself does ──
                iam.PolicyStatement(
                    sid="ReadCompiledIntakeAndClippedSql",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_GET_OBJECT],
                    resources=[f"arn:aws:s3:::{feature_ingestion_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="WriteIngestReport",
                    effect=iam.Effect.ALLOW,
                    # AC-6: the report goes to ingest.output_s3_uri, which is
                    # <fg>/<run id>/processing-output/ in the same bucket.
                    actions=[S3_PUT_OBJECT],
                    resources=[f"arn:aws:s3:::{feature_ingestion_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="EcrAuthToken",
                    effect=iam.Effect.ALLOW,
                    # The job cannot pull its own image without this, and
                    # GetAuthorizationToken takes no resource.
                    actions=[ECR_GET_AUTHORIZATION_TOKEN],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="EcrImageAccess",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                        ECR_BATCH_GET_IMAGE,
                        ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                    ],
                    resources=[
                        f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="CloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    # SageMaker writes Processing job output to its own service
                    # log group
                    actions=[
                        LOGS_CREATE_LOG_GROUP,
                        LOGS_CREATE_LOG_STREAM,
                        LOGS_PUT_LOG_EVENTS,
                        LOGS_DESCRIBE_LOG_STREAMS,
                    ],
                    resources=[
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/sagemaker/ProcessingJobs:*"
                    ],
                ),
                iam.PolicyStatement(
                    sid="VpcNetworkInterfaces",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        EC2_CREATE_NETWORK_INTERFACE,
                        EC2_CREATE_NETWORK_INTERFACE_PERMISSION,
                        EC2_DELETE_NETWORK_INTERFACE,
                        EC2_DELETE_NETWORK_INTERFACE_PERMISSION,
                        EC2_DESCRIBE_DHCP_OPTIONS,
                        EC2_DESCRIBE_SECURITY_GROUPS,
                        EC2_DESCRIBE_SUBNETS,
                        EC2_DESCRIBE_VPCS,
                    ],
                    # AWS does not support resource-level restrictions for these
                    # EC2 network interface actions.
                    resources=["*"],  # NOSONAR
                ),
            ],
        )
        featurestore_ingest_role.attach_inline_policy(featurestore_ingest_policy)

        ssm.StringParameter(
            self,
            "WorkloadFeatureStoreIngestRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/workload-featurestore-ingest-role-arn",
            string_value=featurestore_ingest_role.role_arn,
            description="WorkloadFeatureStoreIngestRole ARN",
        )

        # ── FeatureStoreReadOnly managed policy (DS persona) ──────────
        # AR-05-c. Read-only Feature Store access for the Data Scientist persona.
        # WorkloadFeatureStoreIngestRole above covers ingestion WRITES; nothing
        # granted a DS the ability to read back the values they just onboarded,
        # which the Studio get_record and Athena time-travel demos depend on.
        #
        # Covers both read paths the M2 LLD gives a DS:
        #   online  store — M2 §5 "Online store -> ... GetRecord"
        #   offline store — M2 §5 "Studio (M1) -> Glue / Athena ... browse and
        #                   inspect feature groups"
        #
        # Authored as a standalone customer-managed policy rather than an inline
        # policy on a role, so the same grant attaches to either an IAM role or an
        # IAM Identity Center permission set once the MLPlatform-DataScientist
        # profile exists (AR-03-b, M1 §6). Nothing is attached here.
        #
        # Strictly narrower than the ingest role: no Snowflake secret read, no
        # sagemaker:PutRecord, no Glue write. The only write is the Athena
        # query-results prefix, which Athena requires to run any query.
        featurestore_read_policy = iam.ManagedPolicy(
            self,
            "FeatureStoreReadOnlyPolicy",
            managed_policy_name=(
                f"{policy_prefix}-{app_prefix}-workload-featurestore-read-policy"
            ),
            description=(
                "Read-only SageMaker Feature Store access (online + offline) for "
                "the NOVOS AR Data Scientist persona"
            ),
            statements=[
                iam.PolicyStatement(
                    sid="FeatureStoreOnlineRead",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:GetRecord",
                        "sagemaker:BatchGetRecord",
                        "sagemaker:DescribeFeatureGroup",
                    ],
                    resources=[f"arn:aws:sagemaker:{region}:{account}:feature-group/*"],
                ),
                iam.PolicyStatement(
                    sid="GlueCatalogRead",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        GLUE_GET_DATABASE,
                        "glue:GetDatabases",
                        GLUE_GET_TABLE,
                        "glue:GetTables",
                        "glue:GetPartition",
                        "glue:GetPartitions",
                    ],
                    resources=[
                        f"arn:aws:glue:{region}:{account}:catalog",
                        f"arn:aws:glue:{region}:{account}:database/*",
                        f"arn:aws:glue:{region}:{account}:table/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="AthenaOfflineStoreQuery",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "athena:StartQueryExecution",
                        "athena:StopQueryExecution",
                        "athena:GetQueryExecution",
                        "athena:GetQueryResults",
                        "athena:GetWorkGroup",
                    ],
                    resources=[f"arn:aws:athena:{region}:{account}:workgroup/*"],
                ),
                iam.PolicyStatement(
                    sid="S3OfflineStoreRead",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        S3_GET_OBJECT,
                        S3_LIST_BUCKET,
                        S3_GET_BUCKET_LOCATION,
                    ],
                    resources=[
                        f"arn:aws:s3:::{offline_store_bucket}",
                        f"arn:aws:s3:::{offline_store_bucket}/*",
                        f"arn:aws:s3:::sagemaker-{region}-{account}",
                        f"arn:aws:s3:::sagemaker-{region}-{account}/feature-store/*",
                        f"arn:aws:s3:::sagemaker-{region}-{account}/01-feature-store/*",
                    ],
                ),
                iam.PolicyStatement(
                    # Athena cannot execute a query without writing its result set
                    # to S3. Scoped to the query-results prefix only — this is the
                    # single write in an otherwise read-only policy.
                    sid="S3AthenaQueryResults",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_GET_OBJECT, S3_PUT_OBJECT],
                    resources=[f"arn:aws:s3:::{mrd_datasets_bucket}/athena-results/*"],
                ),
                iam.PolicyStatement(
                    # Both stores are CMK-encrypted (M2 §4), so a read grant without
                    # KMS returns AccessDenied. GenerateDataKey is required to write
                    # the KMS-encrypted Athena result set.
                    sid="KMSFeatureStoreRead",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        KMS_DECRYPT,
                        KMS_DESCRIBE_KEY,
                        KMS_GENERATE_DATA_KEY,
                    ],
                    resources=[kms_key_arn],
                ),
            ],
        )

        ssm.StringParameter(
            self,
            "FeatureStoreReadOnlyPolicyArn",
            parameter_name=f"/{app_prefix}/{environment}/featurestore-read-policy-arn",
            string_value=featurestore_read_policy.managed_policy_arn,
            description="FeatureStoreReadOnly managed policy ARN (DS persona)",
        )

        execution_role.add_managed_policy(featurestore_read_policy)

        # ── DatasetWrite managed policy ───────────────────────────────
        # NTU-1038 / SPEC-M4 §4.4. The dataset area for the model-ready-dataset
        # pipeline is one bucket, so this is one policy over it. Same-account
        # attachment, not a cross-account assume: the dra66_* ARNs in the env JSON
        # are the post-MVP shape and stay unwired.
        #
        # It carries the READS as well as the writes, because the "dataset area" is
        # a single bucket that every MRD stage both reads and writes: the PIT join
        # reads the compiled intake stage 1 wrote and stages its Athena results
        # here, the split job reads the PIT join's output, and the validate job
        # reads all three partitions before it writes the versioned dataset.
        dataset_write_policy = iam.ManagedPolicy(
            self,
            "DatasetWritePolicy",
            managed_policy_name=f"{policy_prefix}-{app_prefix}-dataset-write-policy",
            description=(
                "Read/write access to the NOVOS AR model-ready-dataset area for the "
                "M4 model-ready-dataset Processing jobs"
            ),
            statements=[
                iam.PolicyStatement(
                    sid="DatasetAreaReadWrite",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        S3_GET_OBJECT,
                        S3_PUT_OBJECT,
                        S3_LIST_BUCKET,
                        S3_GET_BUCKET_LOCATION,
                        # A parquet partition can exceed the single-PUT limit, and a
                        # failed multipart upload otherwise leaves a billed orphan.
                        S3_ABORT_MULTIPART_UPLOAD,
                    ],
                    resources=[
                        f"arn:aws:s3:::{mrd_datasets_bucket}",
                        f"arn:aws:s3:::{mrd_datasets_bucket}/*",
                    ],
                ),
                iam.PolicyStatement(
                    # The bucket denies a non-KMS put, so a write grant without
                    # GenerateDataKey fails rather than silently downgrading.
                    sid="DatasetAreaKMS",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        KMS_DECRYPT,
                        KMS_ENCRYPT,
                        KMS_GENERATE_DATA_KEY,
                        KMS_DESCRIBE_KEY,
                    ],
                    resources=[kms_key_arn],
                ),
            ],
        )

        ssm.StringParameter(
            self,
            "DatasetWritePolicyArn",
            parameter_name=f"/{app_prefix}/{environment}/dataset-write-policy-arn",
            string_value=dataset_write_policy.managed_policy_arn,
            description="DatasetWrite managed policy ARN (model-ready-dataset area)",
        )

        execution_role.add_managed_policy(dataset_write_policy)

        # ── WorkloadStudioExecution Role ──────────────────────────────
        # Trusted by sagemaker.amazonaws.com
        # Purpose: Identity used by SageMaker Studio users/kernels to run training
        #          and processing jobs, track experiments, read/write studio artifact
        #          bucket, and pass an approved execution role to SageMaker jobs.
        studio_execution_role = iam.Role(
            self,
            "WorkloadStudioExecutionRole",
            role_name=f"{account_role_prefix}{app_prefix}-workload-studioexecution",
            assumed_by=iam.ServicePrincipal(SAGEMAKER_SERVICE_PRINCIPAL),
            description="SageMaker Studio execution role for NOVOS AR workload development",
            permissions_boundary=permissions_boundary,
        )

        studio_execution_policy = iam.Policy(
            self,
            "WorkloadStudioExecutionPolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-workload-studioexecution-policy",
            statements=[
                iam.PolicyStatement(
                    sid="AllowCreateAndManageTrainingJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreateTrainingJob",
                        "sagemaker:DescribeTrainingJob",
                        "sagemaker:StopTrainingJob",
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:training-job/dummy-training-job-prefix-*"
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowCreateAndManageProcessingJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        SAGEMAKER_CREATE_PROCESSING_JOB,
                        SAGEMAKER_DESCRIBE_PROCESSING_JOB,
                        SAGEMAKER_STOP_PROCESSING_JOB,
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:processing-job/dummy-processing-job-prefix-*"
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowExperimentTrackingIfUsed",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreateExperiment",
                        "sagemaker:DescribeExperiment",
                        "sagemaker:CreateTrial",
                        "sagemaker:DescribeTrial",
                        "sagemaker:CreateTrialComponent",
                        "sagemaker:DescribeTrialComponent",
                        "sagemaker:AssociateTrialComponent",
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:experiment/*",
                        f"arn:aws:sagemaker:{region}:{account}:trial/*",
                        f"arn:aws:sagemaker:{region}:{account}:trial-component/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowStudioArtifactS3Access",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_GET_OBJECT, S3_PUT_OBJECT, "s3:DeleteObject"],
                    resources=[
                        "arn:aws:s3:::dummy-workload-studio-bucket/dummy-studio-artifacts/*"
                    ],
                ),
                iam.PolicyStatement(
                    sid="AllowListStudioArtifactPrefix",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_LIST_BUCKET],
                    resources=["arn:aws:s3:::dummy-workload-studio-bucket"],
                    conditions={
                        "StringLike": {
                            S3_PREFIX_CONDITION_KEY: ["dummy-studio-artifacts/*"]
                        }
                    },
                ),
                iam.PolicyStatement(
                    sid="AllowKmsForStudioArtifactsAndJobs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        KMS_DECRYPT,
                        KMS_ENCRYPT,
                        KMS_GENERATE_DATA_KEY,
                        KMS_DESCRIBE_KEY,
                    ],
                    resources=[kms_key_arn],
                ),
                iam.PolicyStatement(
                    sid="AllowPassOnlyApprovedSageMakerExecutionRole",
                    effect=iam.Effect.ALLOW,
                    actions=[ROLE_DELEGATION_ACTION],
                    resources=[
                        f"arn:aws:iam::{account}:role/dummy-sagemaker-execution-role"
                    ],
                    conditions={
                        "StringEquals": {
                            IAM_PASSED_TO_SERVICE: SAGEMAKER_SERVICE_PRINCIPAL
                        }
                    },
                ),
            ],
        )
        studio_execution_role.attach_inline_policy(studio_execution_policy)

        ssm.StringParameter(
            self,
            "WorkloadStudioExecutionRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/workload-studioexecution-role-arn",
            string_value=studio_execution_role.role_arn,
            description="WorkloadStudioExecution role ARN",
        )

        # ── LambdaExecution Role ──────────────────────────────────────
        # Trusted by lambda.amazonaws.com
        # Purpose: Identity used by Lambda functions. Pulls images from ECR,
        #          writes CloudWatch logs, and manages VPC network interfaces.
        lambda_execution_role = iam.Role(
            self,
            "LambdaExecutionRole",
            role_name=f"{account_role_prefix}{app_prefix}-lambda-execution",
            assumed_by=iam.ServicePrincipal(LAMBDA_SERVICE_PRINCIPAL),
            description="Lambda execution role for NOVOS AR Lambda functions",
            permissions_boundary=permissions_boundary,
        )

        # The baseline every Lambda needs — logs, VPC interfaces, ECR pull, KMS. Held in a
        # local so the per-stage roles below can reuse it instead of restating it.
        lambda_baseline_statements = [
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                effect=iam.Effect.ALLOW,
                actions=[
                    LOGS_CREATE_LOG_GROUP,
                    LOGS_CREATE_LOG_STREAM,
                    LOGS_PUT_LOG_EVENTS,
                    # Needed because this role doubles as the LogRetention custom
                    # resource's role (see cdk_lambdas.py) — CDK's LogRetention
                    # singleton calls these two APIs to manage retention on an
                    # imported (not CDK-created) log group.
                    LOGS_PUT_RETENTION_POLICY,
                    LOGS_DELETE_RETENTION_POLICY,
                ],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/*"],
            ),
            iam.PolicyStatement(
                sid="VpcNetworkInterfaces",
                effect=iam.Effect.ALLOW,
                actions=[
                    EC2_CREATE_NETWORK_INTERFACE,
                    EC2_DESCRIBE_NETWORK_INTERFACES,
                    EC2_DELETE_NETWORK_INTERFACE,
                ],
                # AWS does not support resource-level restrictions for these
                # EC2 network interface actions — wildcard is required for
                # Lambda VPC attachment. Safe: scoped to Lambda execution role only.
                resources=["*"],  # NOSONAR
            ),
            iam.PolicyStatement(
                sid="EcrPull",
                effect=iam.Effect.ALLOW,
                actions=[
                    ECR_GET_AUTHORIZATION_TOKEN,
                ],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="EcrImageAccess",
                effect=iam.Effect.ALLOW,
                actions=[
                    ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                    ECR_BATCH_GET_IMAGE,
                    ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                ],
                resources=[
                    f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}"
                ],
            ),
            iam.PolicyStatement(
                sid="KMSDecrypt",
                effect=iam.Effect.ALLOW,
                actions=[
                    KMS_DECRYPT,
                    KMS_GENERATE_DATA_KEY,
                    KMS_DESCRIBE_KEY,
                ],
                resources=[kms_key_arn],
            ),
        ]

        lambda_execution_policy = iam.Policy(
            self,
            "LambdaExecutionPolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-lambda-execution-policy",
            statements=lambda_baseline_statements
            + [
                iam.PolicyStatement(
                    sid="S3ModelTrainingArtifacts",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        S3_GET_OBJECT,
                        S3_PUT_OBJECT,
                        S3_LIST_BUCKET,
                    ],
                    resources=[
                        f"arn:aws:s3:::{model_training_bucket}",
                        f"arn:aws:s3:::{model_training_bucket}/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="S3MrdDatasetList",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_LIST_BUCKET],
                    resources=[f"arn:aws:s3:::{mrd_datasets_bucket}"],
                ),
                # The launch Lambda's model_training.launch() upserts + starts the
                # training Pipeline itself (SF State 3 only waits), so its role needs
                # the pipeline control-plane calls, not just S3.
                iam.PolicyStatement(
                    sid="SageMakerPipelineControl",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreatePipeline",
                        "sagemaker:UpdatePipeline",
                        "sagemaker:DescribePipeline",
                        "sagemaker:StartPipelineExecution",
                        SAGEMAKER_ADD_TAGS,
                        SAGEMAKER_LIST_TAGS,
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:pipeline/*",
                    ],
                ),
                # ensure_group_datazone_tags() creates + tags the Model Package Group.
                iam.PolicyStatement(
                    sid="SageMakerModelRegistry",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreateModelPackageGroup",
                        "sagemaker:DescribeModelPackageGroup",
                        SAGEMAKER_ADD_TAGS,
                        SAGEMAKER_LIST_TAGS,
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:model-package-group/*",
                    ],
                ),
                # upsert() passes the pipeline's execution role to SageMaker; without
                # PassRole on it, CreatePipeline is denied. This is the role
                # AR_EXECUTION_ROLE_ARN must name for the pipeline to run correctly.
                iam.PolicyStatement(
                    sid="PassPipelineExecutionRole",
                    effect=iam.Effect.ALLOW,
                    actions=[ROLE_DELEGATION_ACTION],
                    resources=[workload_execution_role.role_arn],
                ),
            ],
        )
        lambda_execution_role.attach_inline_policy(lambda_execution_policy)

        ssm.StringParameter(
            self,
            "LambdaExecutionRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/lambda-execution-role-arn",
            string_value=lambda_execution_role.role_arn,
            description="LambdaExecution role ARN",
        )

        # ── FeatureIngestionLambda Role ───────────────────────────────
        # Trusted by lambda.amazonaws.com
        # Purpose: shared execution role for every Lambda stage of the feature
        #          ingestion state machine — the intake compiler, the feature group
        #          creater, and the describe/poll stage. Stage 4 is a SageMaker
        #          Processing job and runs as WorkloadFeatureStoreIngestRole, so it
        #          is deliberately not covered here.
        #
        # cdk_lambdas.py names each function {app_prefix}-{name}-{environment}, so
        # the log group grants below depend on the lambdas block in
        # environments/<env>.json using these names.
        # Stage 3 is a native aws-sdk integration in the state machine, not a
        # Lambda, so there are only two functions to grant log access to.
        feature_ingestion_functions = [
            f"{app_prefix}-feature-intake-compiler-{environment}",
            f"{app_prefix}-feature-group-creater-{environment}",
        ]
        feature_ingestion_log_groups = [
            arn
            for function_name in feature_ingestion_functions
            for arn in (
                f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{function_name}",
                f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{function_name}:*",
            )
        ]

        feature_ingestion_lambda_role = iam.Role(
            self,
            "FeatureIngestionLambdaRole",
            role_name=f"{account_role_prefix}{app_prefix}-lambda-feature-ingestion",
            assumed_by=iam.ServicePrincipal(LAMBDA_SERVICE_PRINCIPAL),
            description="Lambda execution role for the NOVOS AR feature ingestion stages",
            permissions_boundary=permissions_boundary,
        )

        feature_ingestion_lambda_policy = iam.Policy(
            self,
            "FeatureIngestionLambdaPolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-lambda-feature-ingestion-policy",
            statements=[
                iam.PolicyStatement(
                    sid="CloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_GROUP,
                        LOGS_CREATE_LOG_STREAM,
                        LOGS_PUT_LOG_EVENTS,
                    ],
                    # Scoped to these two functions rather than /aws/lambda/*.
                    resources=feature_ingestion_log_groups,
                ),
                iam.PolicyStatement(
                    sid="VpcNetworkInterfaces",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        EC2_CREATE_NETWORK_INTERFACE,
                        EC2_DESCRIBE_NETWORK_INTERFACES,
                        EC2_DELETE_NETWORK_INTERFACE,
                    ],
                    # AWS does not support resource-level restrictions for these
                    # EC2 network interface actions — wildcard is required for
                    # Lambda VPC attachment, and cdk_lambdas.py always attaches.
                    resources=["*"],  # NOSONAR
                ),
                iam.PolicyStatement(
                    sid="EcrAuthToken",
                    effect=iam.Effect.ALLOW,
                    actions=[ECR_GET_AUTHORIZATION_TOKEN],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="EcrImageAccess",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                        ECR_BATCH_GET_IMAGE,
                        ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                    ],
                    resources=[
                        f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="ReadIntakeFormAndCompiledIntake",
                    effect=iam.Effect.ALLOW,
                    # Stage 1 reads the form and its source SQL; stages 2 and 3
                    # read the compiled intake stage 1 wrote. The config bucket is
                    # where a team uploads its feature-intake directory.
                    actions=[S3_GET_OBJECT],
                    resources=[
                        f"arn:aws:s3:::{feature_ingestion_bucket}/*",
                        f"arn:aws:s3:::{ds_datasets_bucket}/*",
                        f"arn:aws:s3:::{config_bucket}/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="ListIntakeBuckets",
                    effect=iam.Effect.ALLOW,
                    # ListBucket so stage 1 can resolve a feature-intake DIRECTORY
                    # to the form and SQL objects inside it.
                    actions=[S3_LIST_BUCKET],
                    resources=[
                        f"arn:aws:s3:::{feature_ingestion_bucket}",
                        f"arn:aws:s3:::{ds_datasets_bucket}",
                        f"arn:aws:s3:::{config_bucket}",
                    ],
                ),
                iam.PolicyStatement(
                    sid="WriteCompiledIntake",
                    effect=iam.Effect.ALLOW,
                    actions=[S3_PUT_OBJECT],
                    resources=[f"arn:aws:s3:::{feature_ingestion_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="ManageFeatureGroups",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreateFeatureGroup",
                        SAGEMAKER_DESCRIBE_FEATURE_GROUP,
                        # The AR-05-d AC-6 delete + recreate path: OfflineStoreConfig
                        # and both stores' CMKs can only be set at creation time.
                        "sagemaker:DeleteFeatureGroup",
                        "sagemaker:UpdateFeatureMetadata",
                        # V17: CreateFeatureGroup carrying Tags fails without a tag
                        # grant, and every group is tagged for discovery.
                        SAGEMAKER_ADD_TAGS,
                        SAGEMAKER_LIST_TAGS,
                    ],
                    resources=[feature_group_arn],
                ),
                iam.PolicyStatement(
                    sid="PassIngestRoleToFeatureStore",
                    effect=iam.Effect.ALLOW,
                    # CreateFeatureGroup hands Feature Store the role it uses to
                    # write the offline store; that is a PassRole.
                    actions=[ROLE_DELEGATION_ACTION],
                    resources=[featurestore_ingest_role.role_arn],
                    conditions={
                        "StringEquals": {
                            IAM_PASSED_TO_SERVICE: SAGEMAKER_SERVICE_PRINCIPAL
                        }
                    },
                ),
                iam.PolicyStatement(
                    sid="KmsForCompiledIntake",
                    effect=iam.Effect.ALLOW,
                    # The compiled intake is written with SSE-KMS and read back by
                    # the later stages, so both directions of the CMK are needed.
                    actions=[
                        KMS_DECRYPT,
                        KMS_ENCRYPT,
                        KMS_GENERATE_DATA_KEY,
                        KMS_DESCRIBE_KEY,
                    ],
                    resources=[kms_key_arn],
                ),
                iam.PolicyStatement(
                    sid="KmsGrantForFeatureStoreOnlineStore",
                    effect=iam.Effect.ALLOW,
                    # A CMK-encrypted online store requires the principal creating
                    # the feature group to grant Feature Store use of the key.
                    actions=["kms:CreateGrant"],
                    resources=[kms_key_arn],
                    conditions={"Bool": {"kms:GrantIsForAWSResource": "true"}},
                ),
                iam.PolicyStatement(
                    sid="GlueCatalogForOfflineStore",
                    effect=iam.Effect.ALLOW,
                    # Feature Store creates the offline store's Glue database and
                    # table as the CALLER, not as the RoleArn in the request.
                    actions=[
                        "glue:CreateDatabase",
                        "glue:CreateTable",
                        GLUE_GET_DATABASE,
                        GLUE_GET_TABLE,
                        "glue:UpdateTable",
                        "glue:DeleteTable",
                    ],
                    # Each feature group catalogs into its own database, named after
                    # the group by the compiler, so the names cannot be enumerated.
                    resources=[
                        f"arn:aws:glue:{region}:{account}:catalog",
                        f"arn:aws:glue:{region}:{account}:database/*",
                        f"arn:aws:glue:{region}:{account}:table/*/*",
                    ],
                ),
            ],
        )
        feature_ingestion_lambda_role.attach_inline_policy(
            feature_ingestion_lambda_policy
        )

        ssm.StringParameter(
            self,
            "FeatureIngestionLambdaRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/lambda-feature-ingestion-role-arn",
            string_value=feature_ingestion_lambda_role.role_arn,
            description="FeatureIngestionLambda role ARN",
        )

        # ── CohortLabelLambda Role ────────────────────────────────────
        # Trusted by lambda.amazonaws.com
        # Purpose: the cohort-label intake compiler (stage 1). Mirrors the shape of
        #          FeatureIngestionLambda but strictly narrower: it creates no
        #          feature groups, touches no Glue catalog, and passes no role. Its
        #          whole job is to read a staged intake directory out of the config
        #          bucket and write the compiled intake to the cohort-label bucket.
        #          Stage 2 is a Processing job running as CohortLabelExecution, so it
        #          is deliberately not covered here.
        cohort_label_function = f"{app_prefix}-cohort-validation-{environment}"
        cohort_label_lambda_role = iam.Role(
            self,
            "CohortLabelLambdaRole",
            role_name=f"{account_role_prefix}{app_prefix}-lambda-cohort-label",
            assumed_by=iam.ServicePrincipal(LAMBDA_SERVICE_PRINCIPAL),
            description="Lambda execution role for the NOVOS AR cohort-label intake compiler",
            permissions_boundary=permissions_boundary,
        )

        cohort_label_lambda_policy = iam.Policy(
            self,
            "CohortLabelLambdaPolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-lambda-cohort-label-policy",
            statements=[
                iam.PolicyStatement(
                    sid="CloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_GROUP,
                        LOGS_CREATE_LOG_STREAM,
                        LOGS_PUT_LOG_EVENTS,
                    ],
                    # Scoped to this one function rather than /aws/lambda/*.
                    resources=[
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{cohort_label_function}",
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{cohort_label_function}:*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="VpcNetworkInterfaces",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        EC2_CREATE_NETWORK_INTERFACE,
                        EC2_DESCRIBE_NETWORK_INTERFACES,
                        EC2_DELETE_NETWORK_INTERFACE,
                    ],
                    resources=["*"],  # NOSONAR
                ),
                iam.PolicyStatement(
                    sid="EcrAuthToken",
                    effect=iam.Effect.ALLOW,
                    actions=[ECR_GET_AUTHORIZATION_TOKEN],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="EcrImageAccess",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                        ECR_BATCH_GET_IMAGE,
                        ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                    ],
                    resources=[
                        f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="ReadStagedIntakeDirectory",
                    effect=iam.Effect.ALLOW,
                    # The form, its sql/ sibling and any local_transformations tree,
                    # staged by hand under cohort-label-intake/<brand>/<model>/.
                    actions=[S3_GET_OBJECT],
                    resources=[f"arn:aws:s3:::{config_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="ListStagedIntakeDirectory",
                    effect=iam.Effect.ALLOW,
                    # ListBucket so the handler can mirror a DIRECTORY into /tmp
                    actions=[S3_LIST_BUCKET],
                    resources=[f"arn:aws:s3:::{config_bucket}"],
                ),
                iam.PolicyStatement(
                    sid="WriteCompiledIntake",
                    effect=iam.Effect.ALLOW,
                    # [Run ID]/compile-output/, read back by the Processing job.
                    actions=[S3_PUT_OBJECT],
                    resources=[f"arn:aws:s3:::{cohort_label_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="KmsForCompiledIntake",
                    effect=iam.Effect.ALLOW,
                    # The intake directory is read from a CMK-encrypted bucket and
                    # the compiled intake is written with SSE-KMS.
                    actions=[
                        KMS_DECRYPT,
                        KMS_ENCRYPT,
                        KMS_GENERATE_DATA_KEY,
                        KMS_DESCRIBE_KEY,
                    ],
                    resources=[kms_key_arn],
                ),
            ],
        )
        cohort_label_lambda_role.attach_inline_policy(cohort_label_lambda_policy)

        ssm.StringParameter(
            self,
            "CohortLabelLambdaRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/lambda-cohort-label-role-arn",
            string_value=cohort_label_lambda_role.role_arn,
            description="CohortLabelLambda role ARN",
        )

        # ── ModelReadyDatasetLambda Role ──────────────────────────────
        # Trusted by lambda.amazonaws.com
        # Purpose: the model-ready-dataset intake compiler (NTU-1038 stage 1). Its own
        #          role.
        #          It reads three things and writes one: the staged MRD intake
        #          directory from the config bucket, the upstream cohort-label run's
        #          compiled intake, and (when source.s3 pins no version) a listing of
        #          the cohort bucket to resolve the latest; it writes
        #          [Run ID]/compile-output/ to the model-ready-dataset bucket.
        #          The three Processing stages run as CohortLabelExecution, so they
        #          are deliberately not covered here.
        mrd_function = f"{app_prefix}-mrd-validation-{environment}"
        mrd_lambda_role = iam.Role(
            self,
            "ModelReadyDatasetLambdaRole",
            role_name=f"{account_role_prefix}{app_prefix}-lambda-mrd",
            assumed_by=iam.ServicePrincipal(LAMBDA_SERVICE_PRINCIPAL),
            description=(
                "Lambda execution role for the NOVOS AR model-ready-dataset intake "
                "compiler"
            ),
            permissions_boundary=permissions_boundary,
        )

        mrd_lambda_policy = iam.Policy(
            self,
            "ModelReadyDatasetLambdaPolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-lambda-mrd-policy",
            statements=[
                iam.PolicyStatement(
                    sid="CloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_GROUP,
                        LOGS_CREATE_LOG_STREAM,
                        LOGS_PUT_LOG_EVENTS,
                    ],
                    # Scoped to this one function rather than /aws/lambda/*.
                    resources=[
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{mrd_function}",
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{mrd_function}:*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="VpcNetworkInterfaces",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        EC2_CREATE_NETWORK_INTERFACE,
                        EC2_DESCRIBE_NETWORK_INTERFACES,
                        EC2_DELETE_NETWORK_INTERFACE,
                    ],
                    resources=["*"],  # NOSONAR
                ),
                iam.PolicyStatement(
                    sid="EcrAuthToken",
                    effect=iam.Effect.ALLOW,
                    actions=[ECR_GET_AUTHORIZATION_TOKEN],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="EcrImageAccess",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        ECR_BATCH_CHECK_LAYER_AVAILABILITY,
                        ECR_BATCH_GET_IMAGE,
                        ECR_GET_DOWNLOAD_URL_FOR_LAYER,
                    ],
                    resources=[
                        f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="ReadStagedIntakeDirectory",
                    effect=iam.Effect.ALLOW,
                    # The form and any local_transformations tree, staged by hand
                    # under model-ready-dataset-intake/<brand>/<model>/.
                    actions=[S3_GET_OBJECT],
                    resources=[f"arn:aws:s3:::{config_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="ListStagedIntakeDirectory",
                    effect=iam.Effect.ALLOW,
                    # ListBucket so the handler can mirror a DIRECTORY into /tmp.
                    actions=[S3_LIST_BUCKET],
                    resources=[f"arn:aws:s3:::{config_bucket}"],
                ),
                iam.PolicyStatement(
                    sid="ReadUpstreamCohortLabelRun",
                    effect=iam.Effect.ALLOW,
                    # The upstream run's compiled intake, which supplies the
                    # target_type, label columns and entity id this form is checked
                    # against. ListBucket resolves `latest` when source.s3 pins no
                    # dataset_version.
                    actions=[S3_GET_OBJECT, S3_LIST_BUCKET],
                    resources=[
                        f"arn:aws:s3:::{cohort_label_bucket}",
                        f"arn:aws:s3:::{cohort_label_bucket}/*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="WriteCompiledIntake",
                    effect=iam.Effect.ALLOW,
                    # [Run ID]/compile-output/{compiled_intake.json,cohort_source.sql},
                    # read back by all three Processing stages.
                    actions=[S3_PUT_OBJECT],
                    resources=[f"arn:aws:s3:::{mrd_datasets_bucket}/*"],
                ),
                iam.PolicyStatement(
                    sid="KmsForCompiledIntake",
                    effect=iam.Effect.ALLOW,
                    # Both buckets are CMK-encrypted and the compiled intake is
                    # written with SSE-KMS.
                    actions=[
                        KMS_DECRYPT,
                        KMS_ENCRYPT,
                        KMS_GENERATE_DATA_KEY,
                        KMS_DESCRIBE_KEY,
                    ],
                    resources=[kms_key_arn],
                ),
            ],
        )
        mrd_lambda_role.attach_inline_policy(mrd_lambda_policy)

        ssm.StringParameter(
            self,
            "ModelReadyDatasetLambdaRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/lambda-mrd-role-arn",
            string_value=mrd_lambda_role.role_arn,
            description="ModelReadyDatasetLambda role ARN",
        )

        # ── FeatureOnboardingStateMachine Role ────────────────────────
        feature_onboarding_state_machine_role = iam.Role(
            self,
            "FeatureOnboardingStateMachineRole",
            role_name=f"{account_role_prefix}{app_prefix}-statemachine-feature-onboarding",
            assumed_by=iam.ServicePrincipal(STATES_SERVICE_PRINCIPAL),
            description="Step Functions execution role for the NOVOS AR feature onboarding state machine",
            permissions_boundary=permissions_boundary,
        )

        feature_onboarding_state_machine_policy = iam.Policy(
            self,
            "FeatureOnboardingStateMachinePolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-statemachine-feature-onboarding-policy",
            statements=[
                iam.PolicyStatement(
                    sid="InvokeFeatureIngestionLambdas",
                    effect=iam.Effect.ALLOW,
                    actions=[LAMBDA_INVOKE_FUNCTION],
                    resources=[
                        f"arn:aws:lambda:{region}:{account}:function:{app_prefix}-feature-intake-compiler-{environment}",
                        f"arn:aws:lambda:{region}:{account}:function:{app_prefix}-feature-group-creater-{environment}",
                    ],
                ),
                iam.PolicyStatement(
                    sid="DescribeFeatureGroupWaitLoop",
                    effect=iam.Effect.ALLOW,
                    actions=[SAGEMAKER_DESCRIBE_FEATURE_GROUP],
                    resources=[feature_group_arn],
                ),
                iam.PolicyStatement(
                    sid="RunIngestProcessingJob",
                    effect=iam.Effect.ALLOW,
                    # Stage 4 has no .sync integration, so the state machine
                    # creates the job and then polls describe itself
                    actions=[
                        SAGEMAKER_CREATE_PROCESSING_JOB,
                        SAGEMAKER_DESCRIBE_PROCESSING_JOB,
                        SAGEMAKER_STOP_PROCESSING_JOB,
                        # V17: a create carrying Tags fails without a tag grant,
                        # and every job is tagged with ar:runid (SPEC §6.3).
                        SAGEMAKER_ADD_TAGS,
                    ],
                    resources=[ingest_processing_job_arn],
                ),
                iam.PolicyStatement(
                    sid="PassIngestRoleToProcessingJob",
                    effect=iam.Effect.ALLOW,
                    # AC-4: the job runs as the ingest role
                    actions=[ROLE_DELEGATION_ACTION],
                    resources=[featurestore_ingest_role.role_arn],
                    conditions={
                        "StringEquals": {
                            IAM_PASSED_TO_SERVICE: SAGEMAKER_SERVICE_PRINCIPAL
                        }
                    },
                ),
                iam.PolicyStatement(
                    sid="StepFunctionsCloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_DELIVERY,
                        LOGS_DELETE_LOG_DELIVERY,
                        LOGS_DESCRIBE_LOG_GROUPS,
                        LOGS_DESCRIBE_RESOURCE_POLICIES,
                        LOGS_GET_LOG_DELIVERY,
                        LOGS_LIST_LOG_DELIVERIES,
                        LOGS_PUT_RESOURCE_POLICY,
                        LOGS_UPDATE_LOG_DELIVERY,
                    ],
                    resources=["*"],  # NOSONAR
                ),
            ],
        )
        feature_onboarding_state_machine_role.attach_inline_policy(
            feature_onboarding_state_machine_policy
        )

        ssm.StringParameter(
            self,
            "FeatureOnboardingStateMachineRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/statemachine-feature-onboarding-role-arn",
            string_value=feature_onboarding_state_machine_role.role_arn,
            description="FeatureOnboardingStateMachine role ARN",
        )

        # ── ModelTrainingStateMachine Role ────────────────────────────
        # The model-training machine invokes the two Lambdas and polls the pipeline
        # execution the launch Lambda started (start=true), so it needs only
        # InvokeFunction + DescribePipelineExecution. StartPipelineExecution and
        # PassRole stay on the launch Lambda's role, not here.
        model_training_state_machine_role = iam.Role(
            self,
            "ModelTrainingStateMachineRole",
            role_name=f"{account_role_prefix}{app_prefix}-statemachine-model-training",
            assumed_by=iam.ServicePrincipal(STATES_SERVICE_PRINCIPAL),
            description="Step Functions execution role for the NOVOS AR model training state machine",
            permissions_boundary=permissions_boundary,
        )

        model_training_state_machine_policy = iam.Policy(
            self,
            "ModelTrainingStateMachinePolicy",
            policy_name=f"{policy_prefix}-{app_prefix}-statemachine-model-training-policy",
            statements=[
                iam.PolicyStatement(
                    sid="InvokeModelTrainingLambdas",
                    effect=iam.Effect.ALLOW,
                    actions=[LAMBDA_INVOKE_FUNCTION],
                    resources=[
                        f"arn:aws:lambda:{region}:{account}:function:{app_prefix}-model-training-pipeline-validate-{environment}",
                        f"arn:aws:lambda:{region}:{account}:function:{app_prefix}-model-training-pipeline-launch-{environment}",
                    ],
                ),
                iam.PolicyStatement(
                    sid="DescribePipelineExecutionWaitLoop",
                    effect=iam.Effect.ALLOW,
                    actions=["sagemaker:DescribePipelineExecution"],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:pipeline/*/execution/*"
                    ],
                ),
                iam.PolicyStatement(
                    sid="StepFunctionsCloudWatchLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        LOGS_CREATE_LOG_DELIVERY,
                        LOGS_DELETE_LOG_DELIVERY,
                        LOGS_DESCRIBE_LOG_GROUPS,
                        LOGS_DESCRIBE_RESOURCE_POLICIES,
                        LOGS_GET_LOG_DELIVERY,
                        LOGS_LIST_LOG_DELIVERIES,
                        LOGS_PUT_RESOURCE_POLICY,
                        LOGS_UPDATE_LOG_DELIVERY,
                    ],
                    resources=["*"],  # NOSONAR
                ),
            ],
        )
        model_training_state_machine_role.attach_inline_policy(
            model_training_state_machine_policy
        )

        ssm.StringParameter(
            self,
            "ModelTrainingStateMachineRoleArn",
            parameter_name=f"/{app_prefix}/{environment}/statemachine-model-training-role-arn",
            string_value=model_training_state_machine_role.role_arn,
            description="ModelTrainingStateMachine role ARN",
        )
