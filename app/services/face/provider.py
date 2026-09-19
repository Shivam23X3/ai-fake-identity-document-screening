"""FaceVerificationProvider implementation wiring the face engine into the seam.

Implements the ``FaceVerificationProvider`` protocol from
``app/services/ai_providers.py``. The stage calls
``verify(document_image_path, probe_image_path)``; this provider returns the
Step-7 contract payload (flat keys for the pipeline seam) plus the richer
``face_verification`` block the frontend FacePanel renders.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.face.engine import FaceImageError, verify as run_verification

logger = logging.getLogger(__name__)


class CvFaceVerificationProvider:
    """Real face-verification provider (Step 7)."""

    name = "cv-face-yunet-sface"

    def verify(self, document_image_path: str, probe_image_path: str | None) -> dict[str, Any]:
        try:
            payload = run_verification(document_image_path, probe_image_path)
        except FaceImageError as exc:
            logger.warning("Face verification failed for %s: %s", document_image_path, exc)
            return self._unavailable(f"Face verification failed: {exc}", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the pipeline stage
            logger.exception("Unexpected face-engine failure for %s", document_image_path)
            return self._unavailable(
                f"Face engine error: {type(exc).__name__}: {exc}", error=str(exc)
            )

        status = payload.get("match_status")
        confidence = payload.get("confidence")
        # Missing weights = the capability itself is unavailable ⇒ the stage
        # must show 'module pending' (implemented False), not a verdict.
        model_missing = any(
            r.get("code") in {"model_unavailable", "embedding_failed"}
            for r in payload.get("reasons", [])
        )
        # Non-MATCH verdicts always need a human decision; MATCH verdicts with
        # degraded confidence do too.
        needs_review = status != "MATCH" or bool(payload.get("reasons")) or (
            isinstance(confidence, (int, float)) and confidence < 0.7
        )

        return {
            "implemented": not model_missing,
            **({"note": "Face-embedding model not available; run scripts/get_face_models.py."} if model_missing else {}),
            "engine": self.name,
            "confidence": confidence,
            # --- Step-7 contract payload (nested, for the frontend) ---
            "face_verification": payload,
            # --- flat contract keys (pipeline seam / API consumers) ---
            "face_detected_document": payload.get("face_detected_document"),
            "face_detected_presented_person": payload.get("face_detected_presented_person"),
            "similarity_score": payload.get("similarity_score"),
            "match_status": status,
            "match": status == "MATCH",
            "explanation": self._explain(payload),
            "human_review_required": needs_review,
            "duration_ms": payload.get("duration_ms"),
            "disclaimer": payload.get("disclaimer"),
        }

    @staticmethod
    def _explain(payload: dict[str, Any]) -> str:
        status = payload.get("match_status")
        sim = payload.get("similarity_score")
        if status == "MATCH":
            return (
                f"Document portrait and presented person are consistent with the same "
                f"person (cosine similarity {sim:.2f}); advisory only."
            )
        if status == "NO_MATCH":
            return (
                f"Document portrait and presented person are NOT consistent with the "
                f"same person (cosine similarity {sim:.2f}); verify manually."
            )
        if sim is not None:
            return (
                f"Similarity {sim:.2f} falls in the gray zone between the match and "
                f"no-match thresholds; human review required."
            )
        reasons = "; ".join(r.get("note", "") for r in payload.get("reasons", []))
        return f"Verification could not complete: {reasons or 'unknown reason'}."

    @staticmethod
    def _unavailable(note: str, error: str | None = None) -> dict[str, Any]:
        return {
            "implemented": False,
            "engine": "cv-face-yunet-sface",
            "note": note,
            "face_detected_document": None,
            "face_detected_presented_person": None,
            "similarity_score": None,
            "match_status": "INCONCLUSIVE",
            "match": False,
            "confidence": None,
            "explanation": "Face verification unavailable; human inspection required.",
            "face_verification": {
                "match_status": "INCONCLUSIVE",
                "similarity_score": None,
                "confidence": None,
                "reasons": [{"code": "engine_error", "note": note}],
            },
            "human_review_required": True,
            **({"error": error} if error else {}),
        }
