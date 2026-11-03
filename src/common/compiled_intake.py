"""Read the compiled intake written by stage 1.

Stages 2, 3 and 4 all start by reading this document, so the parse lives here
rather than in each handler. Nothing is re-derived: a missing key is an error, not
something to recompute from the form.

"""

import json

REQUIRED_KEYS = [
    "run_id",
    "env",
    "feature_group_name",
    "create_feature_group",
    "update_feature_metadata",
    "online_ttl",
    "ingest",
]


class CompiledIntakeError(Exception):
    """The compiled intake is unreadable or was written by an incompatible stage 1."""


def parse(text):
    """Load and check a compiled intake document."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise CompiledIntakeError(f"compiled intake is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise CompiledIntakeError("compiled intake must be a JSON object.")

    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        raise CompiledIntakeError(
            "compiled intake is missing key(s): " + ", ".join(missing)
        )
    return document
