"""Environment-config guards, applied **before** synthesis (CLAUDE.md §CDK rules).

A placeholder that survives synthesis is worse than a missing key: it produces a template that
deploys and then fails, or worse deploys a resource nobody can use. `UNSET-ecr-repository-name`
synthesized into a Lambda `ImageUri` that ECR cannot even accept, in QA and Prod, and every test
passed (F19).

Use `require_configured` for any key where a wrong value yields a deployable-but-broken resource.
Do **not** use it for values that only a runtime consumer reads — the five `AR_SNOWFLAKE_*`
placeholders are deliberately unset until Data Platform supplies them, and failing synthesis on
those would block the dev deploy of everything else for no gain.
"""

from typing import Any

#: A value beginning with this is a placeholder someone has not filled in yet.
UNSET_PREFIX = "UNSET-"


def is_configured(value: Any) -> bool:
    """False for None, empty, whitespace, or an `UNSET-` placeholder."""
    text = str(value or "").strip()
    return bool(text) and not text.startswith(UNSET_PREFIX)


def require_configured(var_config: dict[str, Any], environment: str, key: str) -> str:
    """Return `var_config[key]`, or raise naming the key and the environment."""
    value = var_config.get(key)
    if not is_configured(value):
        raise ValueError(
            f"{key} is not configured in environments/{environment}.json (found {value!r}). "
            "A placeholder here synthesizes into a resource that cannot work — fill it in, or "
            "remove the resource that depends on it, rather than deploying a broken one."
        )
    return str(value).strip()
