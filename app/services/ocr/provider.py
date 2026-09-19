"""Preprocessing + OCR provider implementations for the backend seam.

Implements the ``PreprocessingProvider`` and ``OcrProvider`` protocols in
``app/services/ai_providers.py`` using the independent OCR module. Engines
are initialized lazily so importing this file never loads models.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class OcrPreprocessingProvider:
    """Real preprocessing: rotation/deskew/perspective/resize/denoise/CLAHE."""

    name = "cv2-preprocess"

    def preprocess(self, image_path: str) -> dict[str, Any]:
        from app.services.ocr.preprocessing import PreprocessError, preprocess

        settings = get_settings()
        try:
            result = preprocess(
                image_path,
                target_width=settings.ocr_target_width,
                do_perspective=settings.ocr_enable_perspective,
                do_denoise=settings.ocr_enable_denoise,
                do_contrast=settings.ocr_enable_contrast,
            )
        except PreprocessError as exc:
            logger.warning("Preprocessing failed for %s: %s", image_path, exc)
            return {
                "implemented": False,
                "engine": self.name,
                "note": f"Preprocessing failed: {exc}",
                "preprocessed_image_path": image_path,  # pass through unchanged
                "quality_metrics": None,
            }
        return {
            "implemented": True,
            "engine": self.name,
            "preprocessed_image_path": result.path,
            "quality_metrics": {
                **result.to_dict()["quality"],
                "original_width": result.original_width,
                "original_height": result.original_height,
                "width": result.width,
                "height": result.height,
                "rotation_applied": result.rotation_applied,
                "deskew_angle": result.deskew_angle,
                "perspective_corrected": result.perspective_corrected,
                "blur_removed": result.blur_removed,
                "contrast_gain": result.contrast_gain,
            },
            "warnings": result.warnings,
            "confidence": 0.95,
        }


class RapidOcrProvider:
    """Real OCR: preprocessing-aware extraction via the ocr module."""

    name = "rapidocr"

    def extract(self, image_path: str, doc_type_hint: str | None = None) -> dict[str, Any]:
        from app.services.ocr.engine import OcrEngineError, get_engine
        from app.services.ocr.service import extract_from_image

        settings = get_settings()
        try:
            extraction = extract_from_image(
                image_path,
                doc_type_hint=doc_type_hint,
                engine_name=settings.ocr_engine,
                preprocessed_path=image_path,  # pipeline already preprocessed
            )
        except Exception as exc:  # noqa: BLE001 - report honestly, don't crash pipeline
            logger.exception("OCR provider failed for %s", image_path)
            return {
                "implemented": False,
                "engine": self.name,
                "note": f"OCR failed: {type(exc).__name__}: {exc}",
                "ocr_fields": [],
                "fields": [],
                "mrz_raw": None,
                "doc_type_detected": None,
                "error": str(exc),
                "human_review_required": True,
            }

        if not extraction.success:
            return {
                "implemented": False,
                "engine": self.name,
                "note": f"OCR failed: {extraction.payload.get('error')}",
                "ocr_fields": [],
                "fields": [],
                "mrz_raw": None,
                "doc_type_detected": None,
                "error": extraction.payload.get("error"),
                "human_review_required": True,
            }

        payload = extraction.payload
        # Flat rows for the frontend table + full structure for panels/exports.
        ocr_rows = [
            {
                "name": name,
                "value": fld.get("value") or "",
                "confidence": fld.get("confidence", 0.0),
                "flagged": fld.get("flagged", False),
                "source": fld.get("source"),
                "check_digit_ok": fld.get("check_digit_ok"),
                "notes": fld.get("notes", []),
            }
            for name, fld in payload["fields"].items()
        ]
        return {
            "implemented": True,
            "engine": self.name,
            "confidence": payload.get("overall_confidence"),
            "document_type": payload.get("document_type"),
            "doc_type_detected": payload.get("document_type_detected"),
            "ocr_fields": ocr_rows,
            "fields": payload["fields"],            # structured map (name → {value, confidence, ...})
            "raw_text": payload.get("raw_text", ""),
            "overall_confidence": payload.get("overall_confidence"),
            "mrz_format": payload.get("mrz_format"),
            "mrz_fields": payload.get("mrz"),
            "mrz_raw": payload.get("mrz_raw"),
            "flagged_fields": payload.get("flagged_fields", []),
            "human_review_required": payload.get("human_review_required", True),
            "duration_ms": payload.get("duration_ms"),
            "disclaimer": payload.get("disclaimer"),
        }
