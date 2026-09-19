"""RiskScoringProvider implementation wiring the weighted-rules engine into the seam.

Implements the ``RiskScoringProvider`` protocol from
``app/services/ai_providers.py``. The stage calls ``score(stage_outputs)``;
this provider normalizes the incoming context blocks (the stage passes the
raw ctx keys, including ``doc_type_hint``/``doc_type_detected`` so the
missing-field rules can gate by document type) and returns the Step-8
contract payload. It never raises: a fusion failure degrades to an honest
``implemented: False`` payload with review forced.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.risk.engine import RiskAssessmentError, assess_risk

logger = logging.getLogger(__name__)


class WeightedRulesRiskProvider:
    """Real risk-assessment provider (deterministic weighted rules)."""

    name = "weighted-rules-v2"

    def score(
        self,
        stage_outputs: dict[str, Any],
        stage_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            payload = assess_risk(stage_outputs or {}, stage_results)
        except RiskAssessmentError as exc:
            logger.warning("Risk fusion failed: %s", exc)
            return self._unavailable(f"Risk fusion failed: {exc}", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the pipeline stage
            logger.exception("Unexpected risk-engine failure")
            return self._unavailable(
                f"Risk engine error: {type(exc).__name__}: {exc}", error=str(exc)
            )
        return payload

    @staticmethod
    def _unavailable(note: str, error: str | None = None) -> dict[str, Any]:
        return {
            "implemented": False,
            "engine": WeightedRulesRiskProvider.name,
            "note": note,
            # Step-8 spec keys (honest unknowns)
            "risk_score": None,
            "risk_level": None,
            "reasons": [note],
            "human_review_required": True,
            # compatibility aliases
            "score": None,
            "level": None,
            "band": None,
            "confidence": None,
            "contributions": [],
            "explanation": "Risk assessment unavailable; human review required.",
            "risk": {
                "score": None,
                "level": None,
                "reasons": [note],
                "human_review_required": True,
            },
            **({"error": error} if error else {}),
        }
