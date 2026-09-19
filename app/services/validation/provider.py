"""ValidationProvider implementation wiring the rule engine into the seam.

Implements the ``ValidationProvider`` protocol from
``app/services/ai_providers.py``. The stage calls ``validate(ocr_fields,
doc_type_hint)``; this provider:

1. builds the structured field map (OCR rows *and* the ``fields`` map
   the OCR provider publishes),
2. runs the MOCK registry lookup for the extracted document number
   (clearly stamped ``mock: True`` — demo data only),
3. runs the :class:`ValidationEngine` with the right ruleset,
4. returns the payload in the Step-5 contract shape:
   ``{is_valid, checks[{field, status, message}], confidence}`` plus
   the legacy ``validation``/``failures`` keys the frontend expects.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.services.validation.checks import STATUS_WARNING
from app.services.validation.engine import ValidationEngine

logger = logging.getLogger(__name__)

# Map extracted document-number-ish fields → registry doc_type scope.
_NUMBER_FIELDS: dict[str, list[str]] = {
    "passport": ["passport_number", "document_number"],
    "visa": ["visa_number", "document_number"],
    "national_id": ["document_number", "national_id_number"],
    "driving_license": ["document_number", "license_number"],
    "permit": ["document_number", "permit_number"],
}


def _as_row(name: str, raw: Any) -> dict[str, Any] | None:
    """Normalize one field entry into a row dict, or None if absent."""
    if isinstance(raw, dict):
        value = raw.get("value")
        if value in (None, ""):
            return None
        return {
            "name": name,
            "value": str(value),
            "confidence": float(raw.get("confidence", 0.0) or 0.0),
            "check_digit_ok": raw.get("check_digit_ok"),
        }
    return None


def _registry_lookup(doc_type: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """MOCK registry lookup — clearly-marked demo data, never authoritative."""
    settings = get_settings()
    if not settings.mock_mode:
        return None
    try:
        from app.db.base import get_session_factory
        from app.services.registry_service import lookup_document

        for field_name in _NUMBER_FIELDS.get(doc_type, ["document_number"]):
            value = None
            raw = fields.get(field_name)
            if isinstance(raw, dict):
                value = raw.get("value")
            if value:
                hit = None
                with get_session_factory()() as session:
                    hit = lookup_document(session, doc_type, str(value))
                if hit is not None:
                    return hit
    except Exception as exc:  # noqa: BLE001 - registry hiccups must not break validation
        logger.warning("MOCK registry lookup failed (continuing without): %s", exc)
    return None


class RuleEngineValidationProvider:
    """Real validation provider (Step 5)."""

    name = "rule-engine"

    def __init__(self) -> None:
        self._engine = ValidationEngine()

    def validate(
        self,
        ocr_fields: list[dict],
        doc_type_hint: str,
        mrz_raw: str | None = None,
    ) -> dict[str, Any]:
        """Run validation; ``mrz_raw`` enables MRZ-based checks when present."""
        # ocr_fields rows are the pipeline contract; structured values ride
        # along in the same row dicts (confidence, check_digit_ok).
        fields_map: dict[str, Any] = {}
        for row in ocr_fields or []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            fields_map[row["name"]] = row

        doc_type = (doc_type_hint or "unknown").lower()
        registry = _registry_lookup(doc_type, fields_map)

        # Re-parse the MRZ block so MRZ-based rules can cross-check the
        # extracted fields against the machine-readable zone.
        mrz_fields: dict[str, Any] = {}
        if mrz_raw:
            try:
                from app.services.ocr.mrz import parse_mrz

                parsed = parse_mrz([ln for ln in mrz_raw.splitlines() if ln.strip()])
                if parsed is not None:
                    mrz_fields = {
                        key: {"value": mf.value, "check_digit_ok": mf.check_digit_ok}
                        for key, mf in parsed.fields.items()
                    }
            except Exception as exc:  # noqa: BLE001 - MRZ parse issues must not break validation
                logger.warning("MRZ re-parse for validation failed: %s", exc)

        try:
            outcome = self._engine.validate(
                fields_map,
                doc_type=doc_type,
                doc_type_hint=doc_type_hint,
                mrz_fields=mrz_fields,
                mrz_raw=mrz_raw,
                registry_lookup=registry,
                ocr_fields=ocr_fields or [],
            )
        except Exception as exc:  # noqa: BLE001 - honest failure, never a crash
            logger.exception("validation engine failed")
            return {
                "implemented": True,
                "engine": self.name,
                "is_valid": False,
                "checks": [{
                    "field": "engine", "status": STATUS_WARNING,
                    "message": f"Validation engine error: {type(exc).__name__}: {exc}",
                }],
                "failures": [],
                "confidence": 0.0,
                "human_review_required": True,
                "validation": {
                    "is_valid": False,
                    "checks": [],
                    "failures": [f"validation engine error: {exc}"],
                    "registry": None,
                },
            }

        checks = [c.to_dict() for c in outcome.checks]
        failures = outcome.failures
        warnings = outcome.warnings
        human_review = bool(failures) or bool(warnings) or not outcome.is_valid

        validation_block = {
            "is_valid": outcome.is_valid,
            "checks": checks,
            "confidence": outcome.confidence,
            "doc_type": outcome.doc_type,
            "ruleset": outcome.ruleset,
            "failures": failures,
            "warnings": warnings,
            "registry": registry,
            "notes": outcome.notes,
        }
        return {
            "implemented": True,
            "engine": self.name,
            "confidence": outcome.confidence,
            # --- Step-5 contract payload ---
            "is_valid": outcome.is_valid,
            "checks": checks,
            # --- compatibility keys for stage/frontend/risk ---
            "failures": failures,
            "warnings": warnings,
            "human_review_required": human_review,
            "doc_type_validated": outcome.doc_type,
            "ruleset": outcome.ruleset,
            "registry": registry,
            "notes": outcome.notes,
            "validation": validation_block,
            "disclaimer": (
                "Validation uses generic, simplified rules and clearly-marked MOCK "
                "registry data. It does NOT represent any country's official "
                "document rules; every FAIL/WARNING must be reviewed by a human."
            ),
        }
