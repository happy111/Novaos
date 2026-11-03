"""datasets.py — resolve the ONE upstream (M4) dataset in S3 into train channels.

The split is done UPSTREAM by M4. By the time this pipeline runs, a single
labeled dataset already exists in the MRD (model-ready-dataset) bucket:

    s3://novartis-{account_id}-{env}-{region}-{app_prefix}-mrd-datasets/
        {slug(model_name)}/{data.source.s3_prefix}/{version}/
            train.parquet
            validation.parquet
            holdout.parquet
            manifest.json

The dataset BUCKET is a fixed platform naming convention derived from account
+ env (config.dataset_bucket_name()) — never authored per-model. Within it,
the YAML names the dataset by NAME (data.source.dataset_name, defaulting to
the model name), by S3 PREFIX (data.source.s3_prefix), and by VERSION
(data.source.dataset_version, defaulting to "latest") — {version} is a
LITERAL folder name, not a lexicographic pick among timestamped siblings: the
platform writes/overwrites one folder per version name (typically "latest"),
so this module trusts the named folder directly rather than listing versions.

This module resolves that fixed path and mounts each split file as its own
SageMaker input channel at /opt/ml/input/data/<split>/. M5 never re-splits
(the split is M4's).
"""

from . import config, naming

MANIFEST_FILE = "manifest.json"

# Split -> the parquet file M4 writes for it. 'train' is required; validation
# and holdout are mounted when present. (M4's split is train/validation/holdout.)
SPLIT_FILES = {
    "train": "train.parquet",
    "validation": "validation.parquet",
    "holdout": "holdout.parquet",
}


def _objects_in(s3, bucket, prefix):
    """Set of object keys (basename) directly under a version prefix."""
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return {obj["Key"][len(prefix) :] for obj in resp.get("Contents", [])}


def resolve_dataset(name, version="latest", s3_client=None, dataset_s3_prefix=""):
    """Resolve the M4 dataset (name + s3_prefix + version) to its S3 folder.

    dataset_s3_prefix is the YAML's data.source.s3_prefix; version is
    data.source.dataset_version ("latest" or a pinned value) — both are
    LITERAL path segments, not resolved by listing/comparing sibling
    folders. The dataset bucket is config.dataset_bucket_name(), a fixed
    platform naming convention (not the pipeline's own artifact bucket).

    Returns (version_uri, chosen_version, files) where `files` is the set of
    object basenames present in that folder.

    Raises FileNotFoundError if the named folder has no manifest.json (i.e.
    no VALID M4 output) — business checks the JSON Schema cannot express.
    """
    if not name:
        raise ValueError(
            "dataset name is empty (data.source.dataset_name / model_name)"
        )
    if not dataset_s3_prefix:
        raise ValueError(
            "dataset_s3_prefix is empty (data.source.s3_prefix in the YAML)"
        )
    s3 = s3_client or config.session().client("s3")
    bucket = config.dataset_bucket_name()
    chosen = version or "latest"
    # Slugged to match the folder M4 actually wrote — see naming.dataset_dir_name.
    base_prefix = f"{naming.dataset_dir_name(name)}/{dataset_s3_prefix}/{chosen}/"

    files = _objects_in(s3, bucket, base_prefix)
    if MANIFEST_FILE not in files:
        raise FileNotFoundError(
            f"dataset '{name}' version '{chosen}' not found under "
            f"s3://{bucket}/{base_prefix} (no {MANIFEST_FILE} present — "
            "not a valid M4 output)."
        )

    version_uri = f"s3://{bucket}/{base_prefix}"
    return version_uri, chosen, files


def resolve_channels(data_cfg, s3_client=None):
    """Resolve the `data` block to a channel -> {s3_uri, version, split} map.

    Reads dataset_name (+ optional dataset_version) and dataset_s3_prefix,
    resolves the dataset's fixed S3 folder, and maps each split parquet
    present (train required, validation/holdout optional) to its own
    SageMaker channel. Each channel's s3_uri is the exact split file, mounted
    at /opt/ml/input/data/<split>/.
    """
    name = data_cfg.get("dataset_name", "")
    version = data_cfg.get("dataset_version", "latest")
    dataset_s3_prefix = data_cfg.get("dataset_s3_prefix", "")
    version_uri, chosen, files = resolve_dataset(
        name, version, s3_client=s3_client, dataset_s3_prefix=dataset_s3_prefix
    )

    channels = {}
    for split, filename in SPLIT_FILES.items():
        if filename in files:
            channels[split] = {
                "s3_uri": f"{version_uri}{filename}",
                "version": chosen,
                "split": split,
            }
    if "train" not in channels:
        raise FileNotFoundError(
            f"dataset '{name}' version '{chosen}' has no {SPLIT_FILES['train']} "
            f"(present: {sorted(files)}); a train split is required."
        )
    return channels
