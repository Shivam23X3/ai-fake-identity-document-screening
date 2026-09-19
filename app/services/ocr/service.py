"""High-level OCR service — the module's public API.

    Image → preprocessing → OCR → text cleanup → field extraction
          → confidence calculation → structured JSON

Can be used standalone (import + one call) or through the backend
pipeline via the provider in ``app/services/ocr/provider.py``.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from app.services.ocr import preprocessing
from app.services.ocr.cleanup import cleanup_lines
from app.services.ocr.constants import DEFAULT_DOC_THRESHOLD, MIN_OVERALL_FOR_AUTO_OK
from app.services.ocr.engine import OcrEngineError, OcrOutput, get_engine
from app.services.ocr.fields import ExtractionResult, FieldExtractor
from app.services.ocr.mrz import parse_mrz

logger = logging.getLogger(__name__)

# Header keywords → document type (checked against the first lines).
_DOC_HEADERS: list[tuple[str, str]] = [
    ("PASSPORT", "passport"),
    ("PASSEPORT", "passport"),
    ("PASAPORTE", "passport"),
    ("VISA", "visa"),
]


def detect_document_type(ocr_lines: list[str], hint: str | None) -> str | None:
    """Detect doc type from visible headers; ``hint`` wins when specific."""
    if hint and hint != "unknown":
        return hint
    head = " ".join(ocr_lines[:6]).upper()
    for keyword, doc_type in _DOC_HEADERS:
        if keyword in head:
            return doc_type
    return None


class OcrExtraction:
    """Aggregated output of one full extraction run."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    # Convenience accessors
    @property
    def success(self) -> bool:
        return bool(self.payload.get("success"))

    @property
    def document_type(self) -> str | None:
        return self.payload.get("document_type")

    @property
    def fields(self) -> dict[str, dict[str, Any]]:
        return self.payload.get("fields", {})

    @property
    def overall_confidence(self) -> float | None:
        return self.payload.get("overall_confidence")

    @property
    def human_review_required(self) -> bool:
        return bool(self.payload.get("human_review_required"))


def extract_from_image(
    image_path: str,
    *,
    doc_type_hint: str | None = None,
    output_dir: str | Path | None = None,
    rotation: float = 0.0,
    engine_name: str = "auto",
    preprocessed_path: str | None = None,
    quality_metrics: dict[str, float] | None = None,
) -> OcrExtraction:
    """Run the full OCR extraction pipeline on one document image.

    Parameters mirror the preprocessing options; when
    ``preprocessed_path`` is given (pipeline mode) the image is used as-is
    and preprocessing is skipped.
    """
    started = time.perf_counter()
    warnings: list[str] = []

    # --- 1-2. Preprocessing ---------------------------------------------
    if preprocessed_path:
        pre = None
    else:
        pre = preprocessing.preprocess(
            image_path, output_dir=output_dir, rotation=rotation,
        )
        image_path = pre.path
        warnings.extend(pre.warnings)

    # --- 3. OCR engine ---------------------------------------------------
    try:
        engine = get_engine(engine_name)
    except OcrEngineError as exc:
        return OcrExtraction(_failure(exc, image_path))

    import cv2  # local import: module usable without cv2 when cached results exist

    data = cv2.imread(image_path)
    if data is None:
        return OcrExtraction(_failure(
            RuntimeError(f"Could not read preprocessed image: {image_path}"), image_path,
        ))
    ocr: OcrOutput = engine.run(data)

    # --- 4. Text cleanup ---------------------------------------------------
    report = cleanup_lines(ocr.lines)
    cleaned_lines = report.cleaned_lines
    ocr.raw_text = "\n".join(cleaned_lines)

    # --- 5. Document type detection + MRZ ---------------------------------
    doc_type_detected = detect_document_type(cleaned_lines, doc_type_hint)
    mrz = parse_mrz(cleaned_lines)

    # --- 6. Field extraction -----------------------------------------------
    extractor = FieldExtractor(doc_type_hint or "unknown")
    extraction: ExtractionResult = extractor.extract(ocr, mrz, doc_type_detected)
    if mrz is None and (doc_type_hint or "unknown") == "unknown":
        # No MRZ and no header: downgrade to honest "unknown".
        extraction.document_type = extraction.document_type or (
            doc_type_detected or "unknown"
        )

    # --- 7. Confidence + review routing ------------------------------------
    overall = extraction.overall_confidence()
    flagged_fields = [n for n, f in extraction.fields.items() if f.flagged]
    human_review = (
        bool(flagged_fields)
        or overall is None
        or overall < DEFAULT_DOC_THRESHOLD
        or overall < MIN_OVERALL_FOR_AUTO_OK  # conservative: always review Step-4 output
    )
    if pre and pre.deskew_angle:
        warnings.append(f"deskewed by {pre.deskew_angle}°")

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload: dict[str, Any] = {
        "success": True,
        "document_type": extraction.document_type,
        "document_type_detected": doc_type_detected,
        "doc_type_hint": doc_type_hint,
        "fields": {
            name: fld.to_dict() for name, fld in extraction.fields.items()
        },
        "raw_text": ocr.raw_text,
        "overall_confidence": round(overall, 3) if overall is not None else None,
        "mrz": extraction.to_dict()["mrz_fields"],
        "mrz_format": extraction.to_dict()["mrz_format"],
        "mrz_raw": extraction.mrz_raw,
        "flagged_fields": flagged_fields,
        "human_review_required": human_review,
        "preprocessing": pre.to_dict() | {"warnings": warnings} if pre else {
            "path": preprocessed_path,
            "quality_metrics": quality_metrics or {},
        },
        "engine": engine.name,
        "cleanup": {
            "changed_lines": report.changed_lines,
            "removed_chars": report.removed_chars,
        },
        "duration_ms": elapsed_ms,
        "disclaimer": (
            "OCR output is advisory. Low-confidence fields are flagged and must be "
            "verified against the document image by authorized personnel."
        ),
    }
    return OcrExtraction(payload)


def _failure(exc: Exception, image_path: str) -> dict[str, Any]:
    """Honest failure payload — no fabricated fields."""
    logger.exception("OCR extraction failed for %s", image_path)
    return {
        "success": False,
        "error": f"{type(exc).__name__}: {exc}",
        "document_type": None,
        "fields": {},
        "raw_text": "",
        "overall_confidence": None,
        "human_review_required": True,
        "flagged_fields": [],
    }
