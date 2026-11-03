"""onboarding_form.py — parse model-onboarding-form.yaml into model-card fields.

FORM #1 of the platform's four intake forms (submitted ONCE per model, before
development, by the DS + Business Sponsor). The MVP reads it for ONE purpose:
seeding the SageMaker Model Package's Model Card at registration (AR-08-c). Only
the sections that map to card content are surfaced; the rest are ignored.

Pure like training_config.load — no AWS, no SDK shapes, unit-testable. The
mapping from these fields to a ModelPackageModelCard lives in model_training.py
(where the SDK card shapes are imported); this module just yields typed strings.
"""

from dataclasses import dataclass, field

import yaml


def _text(value) -> str:
    """Collapse a folded/literal YAML scalar into a single trimmed line."""
    return " ".join(str(value or "").split())


def _threshold(value) -> float | None:
    """success_criteria.minimum_threshold as a number. Blank/absent -> None.

    None means "no approved bar declared", which is distinct from 0.0 (a bar every
    value clears) — the cross-file gate check skips the first and honours the second.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"success_criteria.minimum_threshold must be a number, got {value!r}."
        )


@dataclass
class OnboardingForm:
    """The model-card-relevant slice of the onboarding form."""

    model_name: str
    # business
    description: str = ""
    use_case: str = ""
    business_unit: str = ""
    business_sponsor: str = ""
    expected_consumers: list[str] = field(default_factory=list)
    # data
    target_type: str = ""
    # model_card
    purpose: str = ""
    limitations: str = ""
    approved_uses: list[str] = field(default_factory=list)
    prohibited_uses: list[str] = field(default_factory=list)
    fairness_considerations: str = ""
    update_frequency: str = ""
    # governance
    owner_bu: str = ""
    data_classification: str = ""
    # success_criteria — the business-approved bar the model must clear to be
    # approved. Cross-checked against the training config's own gate by
    # validation.validate_kpi_gate(), which needs both files to disagree loudly.
    success_metric: str = ""
    minimum_threshold: float | None = None
    # Where this form was read from, so a cross-file error can name its source.
    source_path: str = ""


def load(path) -> OnboardingForm:
    """Read the onboarding form YAML, return the card-relevant fields.

    model_name is required (it ties the card back to the model identity and is
    cross-checked against the training config by the caller).
    """
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    business = raw.get("business", {}) or {}
    data = raw.get("data", {}) or {}
    card = raw.get("model_card", {}) or {}
    governance = raw.get("governance", {}) or {}
    success = raw.get("success_criteria", {}) or {}

    model_name = business.get("model_name", "")
    if not model_name:
        raise ValueError("business.model_name is required in the onboarding form.")

    return OnboardingForm(
        model_name=model_name,
        description=_text(business.get("description")),
        use_case=_text(business.get("use_case")),
        business_unit=business.get("business_unit", ""),
        business_sponsor=business.get("business_sponsor", ""),
        expected_consumers=business.get("expected_consumers", []) or [],
        target_type=data.get("target_type", ""),
        purpose=_text(card.get("purpose")),
        limitations=_text(card.get("limitations")),
        approved_uses=card.get("approved_uses", []) or [],
        prohibited_uses=card.get("prohibited_uses", []) or [],
        fairness_considerations=_text(card.get("fairness_considerations")),
        update_frequency=_text(card.get("update_frequency")),
        owner_bu=governance.get("owner_bu", ""),
        data_classification=governance.get("data_classification", ""),
        success_metric=(success.get("primary_metric") or "").strip(),
        minimum_threshold=_threshold(success.get("minimum_threshold")),
        source_path=str(path),
    )
