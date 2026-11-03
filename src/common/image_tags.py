"""Compose the ECR tag of a job image whose contents vary by intake form.

A job image that bakes one form's `local_transformations/` tree and that form's
`local_requirements` is not interchangeable with another form's, so it must not
share a tag with one. With a single fixed tag per pipeline, the last team to push
owns the tag: every other team's run then pulls an image that does not contain its
transformations, and fails on `ModuleNotFoundError` — or, when two teams happen to
name a module the same thing, silently runs the other team's code.

The tag therefore carries the form's own identity: the feature group for feature
ingestion, the model for the M4 pipelines.

ONE derivation, called from both sides — the compiler that writes `image_uri` into
the compiled intake, and the `build_and_push.sh` that pushes the image that URI
names. Two implementations of this string, one in Python and one in bash, would
drift and fail exactly the way a shared tag fails, so the shell scripts call
`job_image_tag` through `python3 -` rather than composing it themselves.

Composing a tag here does not make the image exist. A form whose image was never
built resolves to a tag nothing was pushed to, and the run fails when SageMaker pulls
it — loudly, which is the point: the alternative is a run that quietly uses another
team's code. `build_and_push.sh` prints the tag it pushed for the comparison.
"""

import hashlib
import re

# An ECR tag is 1-128 characters of [A-Za-z0-9_.-] and may not lead with . or -.
# Only lowercase alphanumerics and hyphens are produced here, which is a subset.
IMAGE_TAG_MAX_LEN = 128

# The identity segment is capped well inside that limit so the prefix always fits.
# 64 also matches what the identity fields are themselves bounded by: a registered
# feature group name is <= 64 characters (intake_schema.FEATURE_GROUP_NAME_MAX_LEN).
KEY_MAX_LEN = 64

# A key over the cap keeps a readable head plus a digest of the WHOLE key.
# Truncation alone would let two identities that share a 64-character prefix land
# on one tag, which is the collision this module exists to prevent.
_DIGEST_LEN = 8

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def tag_key(value):
    """One form identity, made safe for an ECR tag segment.

    Lowercased, with every run of non-alphanumerics collapsed to a single hyphen,
    so `imm_cos_hs_comm_claims_hcp_mn` becomes `imm-cos-hs-comm-claims-hcp-mn` and
    a model's `brand_abc/patient_outcome_mortality_v2` becomes
    `brand-abc-patient-outcome-mortality-v2`.
    """
    key = _NON_ALNUM_RE.sub("-", str(value).strip().lower()).strip("-")
    if not key:
        raise ValueError(
            f"cannot derive an image tag from {value!r}: it has no alphanumerics."
        )
    if len(key) > KEY_MAX_LEN:
        digest = hashlib.sha256(key.encode()).hexdigest()[:_DIGEST_LEN]
        head = key[: KEY_MAX_LEN - _DIGEST_LEN - 1].rstrip("-")
        key = f"{head}-{digest}"
    return key


def job_image_tag(base_tag, key):
    """`<base_tag>-<key>`: the tag of the image built for that one form.

    `base_tag` is the pipeline's tag from the environment file — the image every
    form of that pipeline is built from — and `key` is the form's identity.
    """
    tag = f"{base_tag}-{tag_key(key)}"
    if len(tag) > IMAGE_TAG_MAX_LEN:
        raise ValueError(
            f"composed image tag {tag!r} is {len(tag)} characters; ECR allows "
            f"{IMAGE_TAG_MAX_LEN}. Shorten the base tag."
        )
    return tag
