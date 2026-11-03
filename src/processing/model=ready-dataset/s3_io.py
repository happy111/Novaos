"""S3 access and structured logging, shared by the three model-ready-dataset stages.

Extracted once all three stages needed the same four helpers. The two earlier stages
carried their own copies while there were two of them, on the standing decision that
deduplication is deferred rather than done as a side effect of a build (stage-1 handoff
§6.5); a third copy is where that trade flips, so the helpers moved here and `pit_join`,
`split` and `validate_and_write` all import them.

Nothing is read or written through a Processing channel. Every object goes through
boto3 so that `ExpectedBucketOwner` is on every call, which a `ProcessingInput`
download cannot be, and so that the same code covers a Snowflake query and an Athena
read of the offline store — neither of which is mountable as a channel at all.
"""

import json


class S3Error(Exception):
    """An S3 URI a stage cannot address: not an s3:// URI, or missing bucket or key."""


def log(message, **fields):
    """Structured log line. runId is on every line, per X5 (SPEC-M4 §6.3)."""
    print(json.dumps({"message": message, **fields}, default=str))


def split_uri(uri, field):
    """Bucket and key from an s3:// URI, named by the field it came from.

    `field` is the compiled intake's own path to the value, so a malformed URI says
    which field produced it — the document carries a dozen of them.
    """
    if not str(uri or "").startswith("s3://"):
        raise S3Error(f"{field}: {uri!r} must be an s3:// URI.")
    bucket, _, key = uri[len("s3://") :].partition("/")
    if not bucket or not key:
        raise S3Error(f"{field}: {uri!r} must include a bucket and a key.")
    return bucket, key


def read_bytes(s3, uri, field, account_id):
    """Read an S3 object's bytes, refusing a bucket this account does not own."""
    bucket, key = split_uri(uri, field)
    response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account_id)
    return response["Body"].read()


def read_text(s3, uri, field, account_id):
    """The same read, decoded — the compiled intake and the source SQL are text."""
    return read_bytes(s3, uri, field, account_id).decode("utf-8")


def put_object(s3, uri, body, content_type, field, config):
    """Write one object, KMS-encrypted, to a bucket this account owns.

    The bucket denies non-KMS puts, so the encryption arguments are not a default
    being restated — omitting them is a hard failure rather than a silent downgrade.
    """
    bucket, key = split_uri(uri, field)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=config.kms_key_arn,
        ExpectedBucketOwner=config.account_id,
    )
    return f"s3://{bucket}/{key}"
