"""The validation engine: runs a ruleset over extracted fields.

Pure orchestration — rules and rulesets are data/callables registered in
``rules.py`` / ``rulesets.py``. The engine:

1. resolves the ruleset (country-specific → generic → unknown),
2. executes every rule with failure isolation (a crashing rule becomes
   a WARNING check, never a pipeline error),
3. aggregates checks into ``is_valid`` (True iff no FAIL),
4. computes a transparent confidence (mean OCR confidence of the
   fields the ruleset actually examined — no invented precision).
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.validation.checks import (
    STATUS_FAIL,
    STATUS_WARNING,
    ValidationCheck,
    ValidationOutcome,
)
from app.services.validation.rulesets import get_ruleset

logger = logging.getLogger(__name__)


class ValidationEngine:
    """Configurable rule engine for document field validation."""

    name = "rule-engine"

    def __init__(self, registry_lookup=None) -> None:
        # Optional async-compatible callable looked up in a thread by the
        # provider; kept engine-side sync — the provider decides execution.
        self._registry_lookup = registry_lookup

    # -- public -------------------------------------------------------------
    def validate(
        self,
        fields: dict[str, Any],
        *,
        doc_type: str,
        doc_type_hint: str | None = None,
        country: str | None = None,
        mrz_fields: dict[str, Any] | None = None,
        mrz_raw: str | None = None,
        registry_lookup: dict[str, Any] | None = None,
        ocr_fields: list[dict[str, Any]] | None = None,
    ) -> ValidationOutcome:
        """Run the resolved ruleset. ``fields`` is the structured field map."""
        ctx: dict[str, Any] = {
            "doc_type": doc_type,
            "doc_type_hint": doc_type_hint,
            "country": country,
            "mrz_fields": mrz_fields or {},
            "mrz_raw": mrz_raw,
            "ocr_fields": ocr_fields or [],
        }
        ruleset = get_ruleset(doc_type, country)
        if ruleset is None:
            return ValidationOutcome(
                is_valid=False,
                checks=[ValidationCheck(
                    field="ruleset", status=STATUS_FAIL,
                    message=f"No validation ruleset for document type '{doc_type}'.",
                )],
                confidence=0.0,
                doc_type=doc_type,
            )

        outcome = ValidationOutcome(
            is_valid=True, doc_type=doc_type, ruleset=ruleset.doc_type,
            notes=[f"ruleset: {ruleset.description}"],
        )
        if not ruleset.country_real_rules:
            outcome.notes.append(
                "Generic, simplified rule set — NOT any country's official document rules."
            )

        for rule in ruleset.rules:
            try:
                rule(outcome, fields, ctx)
            except Exception as exc:  # noqa: BLE001 - rule crash ≠ pipeline crash
                logger.exception("validation rule %s crashed", getattr(rule, "__name__", rule))
                outcome.checks.append(ValidationCheck(
                    field="rule", status=STATUS_WARNING,
                    message=f"Rule '{getattr(rule, '__name__', rule)}' failed to run: {exc}",
                    rule=getattr(rule, "__name__", None),
                ))

        outcome.is_valid = all(c.status != STATUS_FAIL for c in outcome.checks)
        outcome.confidence = self._confidence(fields, outcome)
        if registry_lookup is not None:
            outcome.notes.append(self._registry_note(registry_lookup))
        return outcome

    # -- internals ------------------------------------------------------------
    @staticmethod
    def _confidence(fields: dict[str, Any], outcome: ValidationOutcome) -> float:
        """Mean OCR confidence over the fields the checks examined.

        Transparent and honest: confidence reflects input-data quality,
        not a probability that the document is genuine.
        """
        examined: dict[str, None] = {}
        for check in outcome.checks:
            if check.field and check.field not in ("mrz", "rule", "ruleset"):
                examined.setdefault(check.field, None)
        if not examined:
            return 0.0
        total = 0.0
        for name in examined:
            raw = fields.get(name)
            conf = float(raw.get("confidence", 0.0)) if isinstance(raw, dict) else 0.0
            total += conf
        return total / len(examined)

    @staticmethod
    def _registry_note(lookup: dict[str, Any] | None) -> str:
        if lookup is None:
            return "Registry: no MOCK-registry match for this document number (not found ≠ fake)."
        status = str(lookup.get("status", "unknown"))
        if status == "active":
            return "Registry (MOCK): document found with status 'active'."
        return f"Registry (MOCK): document found with status '{status}'."
