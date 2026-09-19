"""TamperingProvider implementation wiring the forensic engine into the seam.

Implements the ``TamperingProvider`` protocol from
``app/services/ai_providers.py``. The stage calls ``analyze(image_path)``;
this provider runs the full forensic pipeline and returns the Step-6
contract payload, including the flat ``signals`` list the frontend
TamperingPanel already renders plus the richer ``tampering`` block.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.tampering.engine import TamperingAnalysisError, run_tampering_analysis

logger = logging.getLogger(__name__)


class ForensicTamperingProvider:
    """Real tampering provider (Step 6)."""

    name = "forensic-heuristics"

    def analyze(self, image_path: str) -> dict[str, Any]:
        try:
            payload = run_tampering_analysis(image_path)
        except TamperingAnalysisError as exc:
            logger.warning("Tampering analysis failed for %s: %s", image_path, exc)
            return {
                "implemented": False,
                "engine": self.name,
                "note": f"Tampering analysis failed: {exc}",
                "signals": [],
                "score": None,
                "confidence": None,
                "explanation": "Tampering analysis failed; human inspection required.",
                "tampering": {"signals": [], "score": None, "confidence": None},
                "human_review_required": True,
                "error": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - never crash the pipeline stage
            logger.exception("Unexpected tampering-engine failure for %s", image_path)
            return {
                "implemented": False,
                "engine": self.name,
                "note": f"Tampering engine error: {type(exc).__name__}: {exc}",
                "signals": [],
                "score": None,
                "confidence": None,
                "explanation": "Tampering analysis failed; human inspection required.",
                "tampering": {"signals": [], "score": None, "confidence": None},
                "human_review_required": True,
                "error": str(exc),
            }

        risk = payload.get("risk_score")
        # Legacy flat score 0..1 for the pipeline/risk seam; the Step-6
        # contract risk score (0-100) stays inside ``tampering``.
        score_0_1 = (risk / 100.0) if isinstance(risk, (int, float)) else None

        return {
            "implemented": True,
            "engine": payload.get("engine", self.name),
            "confidence": payload.get("confidence"),
            # --- Step-6 contract payload (nested) ---
            "tampering": payload,
            # --- compatibility keys for the existing frontend panel ---
            "signals": [
                {
                    "name": f"{ind['type']} ({ind['severity']})",
                    "suspicious": ind.get("severity") != "info",
                    "details": ind.get("note", ""),
                }
                for ind in payload.get("indicators", [])
            ],
            "score": score_0_1,
            "explanation": payload.get("explanation"),
            "human_review_required": payload.get("human_review_required", True),
            "verdict": payload.get("verdict"),
            "duration_ms": payload.get("duration_ms"),
            "disclaimer": payload.get("disclaimer"),
        }
