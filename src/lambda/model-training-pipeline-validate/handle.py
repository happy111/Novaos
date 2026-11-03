"""handler.py — SF State 2 (Build & Launch) Lambda for the model-training workflow.

Thin adapter: the real logic is src.pipelines.model_training.validate() +
launch(). This handler re-materializes the config from S3 (Lambda-to-Lambda state
is not shared, so it re-validates — cheap, and keeps this state correct even if
invoked directly), then builds the pipeline definition, ensures the registry group
is DataZone-tagged, stages the prep script, upserts, and starts one execution.

Because it mutates AWS (upsert) and calls SageMaker, the SF SHOULD retry this
state with backoff on throttling/transient errors — the opposite retry posture
from the Validate state.

Event (from the Step Function, passed through by the Validate state):
    config_s3_uri          : s3:// of model-training-config.yaml            (required)
    onboarding_form_s3_uri : s3:// of model-onboarding-form.yaml            (optional)
    start                  : bool, default true (false => upsert only, no run)

Returns {"pipeline_name": ..., "execution_arn": ...}. The SF's next state waits on
execution_arn via its native .sync SageMaker Pipeline integration.
"""

import os
import sys

# See pipeline-validate/handler.py for why: put the src/-containing ancestor on the path.
_d = os.path.dirname(os.path.abspath(__file__))
while _d != "/" and not os.path.isdir(os.path.join(_d, "src")):
    _d = os.path.dirname(_d)
if _d not in sys.path:
    sys.path.insert(0, _d)

from src.model_training_core import aws  # noqa: E402
from src.pipelines import model_training  # noqa: E402


def lambda_handler(event, context):
    config_uri = event["config_s3_uri"]
    onboarding_uri = event.get("onboarding_form_s3_uri")
    start = event.get("start", True)

    config_path = aws.download_to_tmp(config_uri)
    onboarding_path = aws.download_to_tmp(onboarding_uri) if onboarding_uri else None

    v = model_training.validate(config_path, onboarding_form_path=onboarding_path)
    result = model_training.launch(
        v["cfg"], v["resolved"], onboarding=v["onboarding"], start=start
    )
    return {"statusCode": 200, **result}
