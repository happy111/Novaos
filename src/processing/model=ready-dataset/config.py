"""Environment configuration for the three model-ready-dataset Processing jobs.

The jobs' own environment contract, kept in the stage rather than in
`src/common/config.py` for the same two reasons the cohort-label job keeps its own:
the Snowflake settings are the bulk of it and that module states it carries none,
because no credential may reach a compiled intake (SPEC-M4 §6.5); and
`ModelReadyDatasetConfig` there describes what the compiler and the state machine
need — the dataset bucket, the execution role, the image tag, the SNS topic — none of
which these jobs read, since the compiled intake already names every object they
touch.

Everything arrives as an environment variable set on the Processing job from the
`snowflake.env_vars` and `tags` blocks in `environments/<env>.json`. The two secret
variables hold ARNs, never secret values.

`ATHENA_OUTPUT_S3_URI` is the one variable with no counterpart on the cohort-label
job: the offline store is read through Athena, and an Athena query has nowhere to
put its results unless it is told (V13).
"""

import os

from src.common.config import ConfigError

# ENV catches a document compiled for one environment being run in another;
# ACCOUNT_ID is the ExpectedBucketOwner on every S3 call; KMS_KEY_ARN encrypts
# what the jobs write.
PLATFORM_VARS = ("ENV", "ACCOUNT_ID", "AWS_REGION", "KMS_KEY_ARN")

# Where Athena stages the offline-store query's results. V13: an Athena read of the
# offline store runs with the CALLER's credentials and needs offline access, Glue
# read, a results location and kms:Decrypt — four grants, not one.
ATHENA_OUTPUT_VAR = "ATHENA_OUTPUT_S3_URI"

# The PrivateLink connection, from `snowflake.env_vars`, read only when
# source.source_location is snowflake. SNOWFLAKE_ACCOUNT and SNOWFLAKE_HOST both
# carry the `...privatelink` form, which is what keeps the read off the public
# endpoint.
SNOWFLAKE_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_HOST",
    "SNOWFLAKE_USER_SECRET_ID",
    "SNOWFLAKE_PRIVATE_KEY_SECRET_ID",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
    "SNOWFLAKE_ROLE",
)

REQUIRED = PLATFORM_VARS + (ATHENA_OUTPUT_VAR,)


class JobConfig:
    """The resolved environment the jobs run in."""

    def __init__(self, env):
        self.env = env["ENV"]
        self.account_id = env["ACCOUNT_ID"]
        self.region = env["AWS_REGION"]
        self.kms_key_arn = env["KMS_KEY_ARN"]
        self.athena_output_s3_uri = env[ATHENA_OUTPUT_VAR]
        # Not in REQUIRED: only a snowflake-sourced form needs these, and a form
        # reading the cohort from S3 must not fail on variables it never uses.
        self.snowflake = {name: env[name] for name in SNOWFLAKE_VARS if env.get(name)}

    def require_snowflake(self):
        """The Snowflake settings, refusing a partial block.

        Called only when `source.source_location` is snowflake, so the missing
        variables are named at the point the read needs them rather than failing
        every S3-sourced run as well.
        """
        missing = [name for name in SNOWFLAKE_VARS if not self.snowflake.get(name)]
        if missing:
            raise ConfigError(
                "source.source_location is 'snowflake' but the Processing job is "
                "missing environment variable(s): " + ", ".join(missing)
            )
        return self.snowflake


def load(env=None):
    """Build the config, naming every missing variable in one message."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise ConfigError(
            "missing required environment variable(s): " + ", ".join(missing)
        )
    return JobConfig(env)
