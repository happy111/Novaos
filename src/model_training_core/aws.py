"""aws.py — shared AWS helpers used by Lambda, Processing, and pipeline code.

Per the AR repo structure (NTU-980), src/model_training_core/ is the shared boundary layer; AWS
plumbing that more than one component needs lives here rather than being copied into
each Lambda. Kept deliberately small — add helpers as real reuse appears, not ahead
of it.
"""

import os
from urllib.parse import urlparse

import boto3


def split_s3_uri(uri):
    """s3://bucket/key -> (bucket, key). Raises on a non-s3 URI."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"not an s3:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def download_to_tmp(uri, s3_client=None, dest_dir="/tmp"):  # NOSONAR
    """Download an s3:// object to a local path and return it.

    Lambdas receive config as S3 URIs (the SF passes the committed YAML's location,
    not its bytes); they must materialize the file before the pipeline code — which
    reads from a path — can parse it. /tmp is the only writable Lambda filesystem.
    The basename is preserved so downstream code that keys off the filename still works.
    """
    bucket, key = split_s3_uri(uri)
    s3 = s3_client or boto3.client("s3")
    dest = os.path.join(dest_dir, os.path.basename(key))
    s3.download_file(bucket, key, dest)
    return dest
