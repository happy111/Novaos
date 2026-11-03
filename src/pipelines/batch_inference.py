"""batch_inference.py — M7 (serving): run a Batch Transform job on a deployable
SageMaker Model.

WHERE THIS FITS. model_create.py turns an APPROVED package into a deployable
Model (CreateModel). This is the next M7 action: a Model does nothing until it is
run — a Batch Transform job (CreateTransformJob) points that Model at an S3
population and writes the scores back to S3. So the M7 chain is:
    approved package -> create_model_from_package() -> submit_batch_transform()

WHY IT LIVES IN src/ (not scripts/). Same split model_create.py and
model_training.py use: the logic that a future Lambda calls lives here; scripts/
is only a thin dev CLI (nothing imports scripts/). When batch inference is
wired to fire automatically (e.g. off an M3 feature refresh, or on demand), its
Lambda handler resolves the Model name + input prefix and calls
submit_batch_transform() — no logic is duplicated into the Lambda.

I/O CONTRACT (fixed by the platform serving image, job-inference-{cpu,gpu}/
serving/app.py — the SAME app real-time and batch both hit):
  input  : CSV, feature values in the trained column order ->
           ContentType=text/csv. split_type picks how SageMaker delivers it:
             "Line" (default) — split by newline into mini-batches; the right
               choice for a HEADERLESS positional file, where any line is
               independently meaningful and where SageMaker cuts does not
               matter.
             None — deliver each S3 OBJECT whole, never split. REQUIRED for a
               model whose inference.py reads a HEADER (a model-ready-dataset
               extract posted as it stands — see
               ar_transformations.inference.base.ModelServer): a header
               travels with line-split's first mini-batch only, so every
               later one arrives headerless and fails to parse. Point input_s3
               at a PREFIX of several objects, each self-contained (its own
               header + rows) and under MaxPayloadInMB, rather than one big
               object split's mini-batches would have handled instead.
  output : one line per instance, "<label>,<probability>"  ->  AssembleWith=Line
SageMaker enumerates EVERY object under the input S3 prefix and writes one
<input-object>.out under the output prefix per input object (it owns the leaf
name; we only choose the prefix).

KMS. Reuses config.KMS_KEY_ARN (same CMK the training artifacts use) for the
transform output + instance volume, falling back to SSE-S3 when unset — exactly
like the pipeline. A caller can override per-run.
"""

from datetime import datetime, timezone

from src.model_training_core import config


def _resolve_output(input_s3, output_s3):
    """Default the output to a batch-output/ sibling of the input prefix.

    SageMaker will not let us name the output OBJECTS (it derives each from the
    matching input object as <object>.out), so callers only ever choose a PREFIX.
    When none is given we hang a batch-output/ folder off the input's parent so a
    run is self-describing without the caller having to invent a path."""
    if output_s3:
        return output_s3
    # input_s3[5:] skips the "s3://" so the bucket-name slash is not counted.
    base = input_s3.rsplit("/", 1)[0] if "/" in input_s3[5:] else input_s3
    return f"{base.rstrip('/')}/batch-output"


def _transform_job_name(model_name, explicit=None):
    """Unique TransformJobName: <model>-batch-<UTC-timestamp> (SageMaker: [A-Za-z0-9-], <=63).

    A UTC timestamp keeps re-runs from colliding AND makes the job name sortable
    and self-dating in the SageMaker console. Truncates the model name to 40 chars
    so <model>(40) + '-batch-'(7) + 'YYYYMMDD-HHMMSS'(15) = 62, under the 63 limit.
    (Two jobs in the same second would collide — pass an explicit name if that
    edge case is possible.)"""
    if explicit:
        return explicit
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{model_name[:40]}-batch-{stamp}"


def submit_batch_transform(
    model_name,
    input_s3,
    *,
    output_s3=None,
    instance_type="ml.m5.large",
    instance_count=1,
    content_type="text/csv",
    accept="text/csv",
    split_type="Line",
    max_payload_mb=None,
    kms_key_arn=None,
    job_name=None,
    session=None,
    wait=False,
):
    """Start a Batch Transform run on an existing deployable Model.

    This is the function a future batch-inference Lambda calls. It does NOT create
    the Model, build images, or approve packages — the Model must already exist
    (model_create.create_model_from_package). It only submits the transform.

    Args:
      model_name: an existing SageMaker Model (from an Approved package).
      input_s3: S3 prefix (or object) of the CSV population to score. For
          split_type="Line" (the default): headerless, trained column order.
          For split_type=None: a prefix of one or more self-contained, headered
          objects (see this module's docstring) — never a single object larger
          than the account's Batch Transform payload ceiling.
      output_s3: S3 prefix for the scores; defaults to a batch-output/ sibling.
      instance_type / instance_count: transform compute (default 1x ml.m5.large).
      content_type / accept: request/response MIME; the platform image speaks CSV.
      split_type: "Line" (default, positional/headerless) or None (deliver each
          S3 object whole — required for a model whose inference.py reads a
          header; see this module's docstring).
      max_payload_mb: TransformResources' MaxPayloadInMB override. Only
          meaningful with split_type="Line" (it caps each mini-batch); SageMaker
          ignores it when split_type is None, since then an object is sent whole
          regardless of size. Default: SageMaker's own default (6).
      kms_key_arn: CMK for the output + instance volume; defaults to
          config.KMS_KEY_ARN, and SSE-S3 when that is also unset.
      job_name: TransformJobName override; defaults to <model>-batch-<rand>.
      session: boto3 Session; defaults to config.session().
      wait: block until the job reaches a terminal state (CLI/dev convenience).

    Returns: {"job_name", "model_name", "input_s3", "output_s3", "status"}.
      status is "Submitted" unless wait=True, then the terminal status.

    Raises: RuntimeError if wait=True and the job ends Failed.
    """
    sess = session or config.session()
    sm = sess.client("sagemaker")

    output_s3 = _resolve_output(input_s3, output_s3)
    job_name = _transform_job_name(model_name, job_name)
    kms_key_arn = kms_key_arn or config.KMS_KEY_ARN  # "" -> SSE-S3

    transform_output = {
        "S3OutputPath": output_s3,
        "Accept": accept,
        "AssembleWith": "Line",
    }
    transform_resources = {
        "InstanceType": instance_type,
        "InstanceCount": instance_count,
    }
    if kms_key_arn:
        transform_output["KmsKeyId"] = kms_key_arn
        transform_resources["VolumeKmsKeyId"] = kms_key_arn

    transform_input = {
        "DataSource": {"S3DataSource": {"S3DataType": "S3Prefix", "S3Uri": input_s3}},
        "ContentType": content_type,
    }
    # SplitType is a required API field; "None" (the string) is its own enum
    # value meaning "do not split" — passing python None would omit the key
    # and fall back to SageMaker's own default ("None"), which is NOT what a
    # caller explicitly passing split_type=None is asking for here, so it is
    # always sent explicitly.
    transform_input["SplitType"] = split_type if split_type else "None"
    create_kwargs = {}
    # MaxPayloadInMB is a CreateTransformJob-level field, not nested under
    # TransformResources. SageMaker rejects it when SplitType is "None" (an
    # unsplit object is sent whole regardless of size), so it is only
    # forwarded with Line splitting.
    if max_payload_mb is not None and transform_input["SplitType"] == "Line":
        create_kwargs["MaxPayloadInMB"] = max_payload_mb

    sm.create_transform_job(
        TransformJobName=job_name,
        ModelName=model_name,
        TransformInput=transform_input,
        TransformOutput=transform_output,
        TransformResources=transform_resources,
        **create_kwargs,
    )
    print(f"[m7  ] submitted transform job '{job_name}'")
    print(f"[m7  ]   model : {model_name}")
    print(f"[m7  ]   input : {input_s3}")
    print(f"[m7  ]   output: {output_s3}/  (one <input-object>.out per input object)")

    status = "Submitted"
    if wait:
        print("[m7  ] waiting for the job to finish ...")
        sm.get_waiter("transform_job_completed_or_stopped").wait(
            TransformJobName=job_name
        )
        desc = sm.describe_transform_job(TransformJobName=job_name)
        status = desc["TransformJobStatus"]
        print(f"[m7  ] transform job '{job_name}' -> {status}")
        if status == "Failed":
            raise RuntimeError(
                f"transform job '{job_name}' failed: "
                f"{desc.get('FailureReason', '(no reason given)')}"
            )

    return {
        "job_name": job_name,
        "model_name": model_name,
        "input_s3": input_s3,
        "output_s3": output_s3,
        "status": status,
    }
