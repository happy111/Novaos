"""CdkNotificationsStack — SNS topics and SQS queues for the NOVOS AR platform.

Config-driven: add topics/queues by editing environments/*.json — no code change needed.

SNS topics
----------
  All topics have KMS encryption (CMK from config kms_key_arn).
  SSM outputs:
    /{app_prefix}/{env}/sns/{name}/topic-arn

SQS queues
----------
  All queues have KMS encryption (CMK from config).
  Per-queue options (all set in environments/*.json):
    retention_days               — message retention period
    visibility_timeout_seconds   — visibility timeout
    is_sfn_dlq                   — if true, this queue ARN is also published at
                                   /{app_prefix}/{env}/sfn-dlq-queue-arn so that
                                   CdkIngestionStack can attach it to the state machine
    dlq                          — optional dead-letter queue block:
                                     retention_days    — DLQ message retention
                                     max_receive_count — receive attempts before moving to DLQ
    subscribe_to_sns             — optional SNS topic name (from sns_topics list);
                                   creates an SNS→SQS subscription with the necessary
                                   SQS resource policy. null/omit = no subscription.

  SSM outputs (always):
    /{app_prefix}/{env}/sqs/{name}/queue-arn
    /{app_prefix}/{env}/sqs/{name}/queue-url
  SSM outputs (when dlq is configured):
    /{app_prefix}/{env}/sqs/{name}-dlq/queue-arn
    /{app_prefix}/{env}/sqs/{name}-dlq/queue-url
"""

from typing import Any

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_kms as kms
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class CdkNotificationsStack(Stack):
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
        kms_key_arn = var_config["kms_key_arn"]

        notifications_cfg = var_config.get("notifications", {})
        if not notifications_cfg:
            raise ValueError("Missing config value: notifications")

        cmk = kms.Key.from_key_arn(self, "NotificationsCmk", kms_key_arn)

        # ── 1. SNS Topics ─────────────────────────────────────────────
        # Keyed by name so queues can look up their subscription target.
        topic_map: dict[str, sns.Topic] = {}

        for t_cfg in notifications_cfg.get("sns_topics", []):
            name: str = t_cfg["name"]
            construct_id = name.replace("-", " ").title().replace(" ", "") + "Topic"

            topic = sns.Topic(
                self,
                construct_id,
                topic_name=f"{app_prefix}-{name}-{environment}",
                display_name=t_cfg.get("display_name", name),
                master_key=cmk,
            )

            topic_map[name] = topic

            ssm.StringParameter(
                self,
                f"{construct_id}Arn",
                parameter_name=f"/{app_prefix}/{environment}/sns/{name}/topic-arn",
                string_value=topic.topic_arn,
                description=f"ARN of SNS topic {name} ({environment})",
            )

        # ── 2. SQS Queues ─────────────────────────────────────────────
        for q_cfg in notifications_cfg.get("sqs_queues", []):
            name: str = q_cfg["name"]
            construct_id = name.replace("-", " ").title().replace(" ", "") + "Queue"

            # ── Optional DLQ ─────────────────────────────────────────
            dead_letter_queue = None
            dlq_cfg = q_cfg.get("dlq")

            if dlq_cfg:
                dlq_construct_id = construct_id + "Dlq"
                dlq = sqs.Queue(
                    self,
                    dlq_construct_id,
                    queue_name=f"{app_prefix}-{name}-dlq-{environment}",
                    retention_period=Duration.days(dlq_cfg["retention_days"]),
                    encryption=sqs.QueueEncryption.KMS,
                    encryption_master_key=cmk,
                    removal_policy=RemovalPolicy.RETAIN,
                )

                dead_letter_queue = sqs.DeadLetterQueue(
                    max_receive_count=dlq_cfg["max_receive_count"],
                    queue=dlq,
                )

                ssm.StringParameter(
                    self,
                    f"{dlq_construct_id}Arn",
                    parameter_name=f"/{app_prefix}/{environment}/sqs/{name}-dlq/queue-arn",
                    string_value=dlq.queue_arn,
                    description=f"ARN of SQS DLQ for {name} ({environment})",
                )
                ssm.StringParameter(
                    self,
                    f"{dlq_construct_id}Url",
                    parameter_name=f"/{app_prefix}/{environment}/sqs/{name}-dlq/queue-url",
                    string_value=dlq.queue_url,
                    description=f"URL of SQS DLQ for {name} ({environment})",
                )

            # ── Main queue ────────────────────────────────────────────
            queue = sqs.Queue(
                self,
                construct_id,
                queue_name=f"{app_prefix}-{name}-{environment}",
                retention_period=Duration.days(q_cfg["retention_days"]),
                visibility_timeout=Duration.seconds(
                    q_cfg.get("visibility_timeout_seconds", 30)
                ),
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=cmk,
                dead_letter_queue=dead_letter_queue,
                removal_policy=RemovalPolicy.RETAIN,
            )

            # ── SNS → SQS subscription ────────────────────────────────
            # subscribe_to_sns must match a name in sns_topics.
            sns_name = q_cfg.get("subscribe_to_sns")
            if sns_name:
                if sns_name not in topic_map:
                    raise ValueError(
                        f"Queue '{name}' references SNS topic '{sns_name}' "
                        f"which is not defined in sns_topics. "
                        f"Available topics: {list(topic_map)}"
                    )
                topic_map[sns_name].add_subscription(
                    subscriptions.SqsSubscription(
                        queue,
                        raw_message_delivery=True,
                    )
                )

            # ── SSM — main queue always published ─────────────────────
            ssm.StringParameter(
                self,
                f"{construct_id}Arn",
                parameter_name=f"/{app_prefix}/{environment}/sqs/{name}/queue-arn",
                string_value=queue.queue_arn,
                description=f"ARN of SQS queue {name} ({environment})",
            )
            ssm.StringParameter(
                self,
                f"{construct_id}Url",
                parameter_name=f"/{app_prefix}/{environment}/sqs/{name}/queue-url",
                string_value=queue.queue_url,
                description=f"URL of SQS queue {name} ({environment})",
            )

            # ── SFN DLQ — publish dedicated SSM path ──────────────────
            # CdkIngestionStack looks up this path to attach the queue
            # to the state machine as its execution-level DLQ.
            if q_cfg.get("is_sfn_dlq"):
                ssm.StringParameter(
                    self,
                    f"{construct_id}SfnDlqArn",
                    parameter_name=f"/{app_prefix}/{environment}/sfn-dlq-queue-arn",
                    string_value=queue.queue_arn,
                    description=f"SQS queue ARN used as Step Functions execution DLQ ({environment})",
                )
