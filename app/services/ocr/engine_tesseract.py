"""Optional Tesseract-backed OCR provider.

Import and register instead of :class:`RapidOcrProvider` when a deployment
prefers Tesseract (binary must be on PATH). Kept separate so the default
provider has no pytesseract dependency at import time.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class TesseractOcrProvider:
    """OcrProvider implementation backed by the Tesseract engine."""

    name = "tesseract"

    def extract(self, image_path: str, doc_type_hint: str | None = None) -> dict[str, Any]:
        from app.services.ocr.engine import OcrEngineError, get_engine
        from app.services.ocr.service import extract_from_image

        settings = get_settings()
        try:
            engine = get_engine("tesseract")
            if engine.name != "tesseract":
                raise OcrEngineError("Tesseract engine not available")
        except OcrEngineError as exc:
            return {
                "implemented": False,
                "engine": self.name,
                "note": f"Tesseract unavailable: {exc}",
                "ocr_fields": [],
                "fields": [],
                "mrz_raw": None,
                "doc_type_detected": None,
                "human_review_required": True,
            }
        try:
            extraction = extract_from_image(
                image_path,
                doc_type_hint=doc_type_hint,
                engine_name="tesseract",
                preprocessed_path=image_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tesseract OCR failed for %s", image_path)
            return {
                "implemented": False,
                "engine": self.name,
                "note": f"OCR failed: {exc}",
                "ocr_fields": [],
                "fields": [],
                "mrz_raw": None,
                "doc_type_detected": None,
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
                "human_review_required": True,
            }

        payload = extraction.payload
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
            "fields": payload["fields"],
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
