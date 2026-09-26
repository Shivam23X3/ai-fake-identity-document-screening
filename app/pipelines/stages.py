"""The six analytic stages of the document screening pipeline.

Each stage is thin glue: it pulls required inputs from the
PipelineContext, calls the configured AI provider for its capability
(see app/services/ai_providers.py), and wraps the provider payload into
a StageResult. All intelligence lives in providers — swap them via
config without touching stages, pipeline or API.

Confidence policy:
- Providers report their own confidence where meaningful.
- Placeholder providers ⇒ human_review_required=True always.
- Stage errors/skips ⇒ human_review_required=True always.
"""
from __future__ import annotations

from typing import Any

from app.core.pipeline import PipelineContext, StageResult, StageStatus
from app.services import ai_providers


def _clamp_confidence(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return None


def _provider_result(
    stage_name: str,
    payload: dict[str, Any],
    output_keys: tuple[str, ...],
    ctx: PipelineContext,
) -> StageResult:
    """Wrap a provider payload into a StageResult + publish ctx keys."""
    is_placeholder = bool(payload.get("implemented") is False)
    data = dict(payload)
    for key in output_keys:
        if key in data:
            ctx.set(key, data[key])
    # Real providers may request human review themselves (e.g. low OCR
    # confidence); placeholders always require it.
    provider_review = bool(payload.get("human_review_required", False))
    return StageResult(
        stage=stage_name,
        status=StageStatus.NOT_IMPLEMENTED if is_placeholder else StageStatus.OK,
        confidence=_clamp_confidence(payload.get("confidence")),
        data=data,
        human_review_required=is_placeholder or provider_review,
    )


class PreprocessStage:
    name = "preprocess"
    requires: tuple[str, ...] = ()
    provides = ("preprocessed_image_path", "quality_metrics")

    def run(self, ctx: PipelineContext) -> StageResult:
        provider = ai_providers.get_provider("preprocessing")
        payload = provider.preprocess(ctx.get("original_path"))
        return _provider_result(
            self.name, payload, ("preprocessed_image_path", "quality_metrics"), ctx
        )


class OcrStage:
    name = "ocr"
    requires = ("preprocessed_image_path",)
    provides = ("ocr_fields", "mrz_raw", "doc_type_detected")

    def run(self, ctx: PipelineContext) -> StageResult:
        provider = ai_providers.get_provider("ocr")
        payload = provider.extract(
            ctx.get("preprocessed_image_path"),
            doc_type_hint=ctx.get("doc_type_hint"),
        )
        return _provider_result(
            self.name,
            payload,
            ("ocr_fields", "mrz_raw", "doc_type_detected"),
            ctx,
        )


class DocumentValidationStage:
    name = "document_validation"
    requires = ("ocr_fields",)
    provides = ("validation",)

    def run(self, ctx: PipelineContext) -> StageResult:
        provider = ai_providers.get_provider("validation")
        payload = provider.validate(
            ocr_fields=ctx.get("ocr_fields") or [],
            doc_type_hint=ctx.get("doc_type_hint") or "unknown",
            mrz_raw=ctx.get("mrz_raw"),
        )
        return _provider_result(self.name, payload, ("validation",), ctx)


class TamperingStage:
    name = "tampering_detection"
    requires = ("preprocessed_image_path",)
    provides = ("tampering",)

    def run(self, ctx: PipelineContext) -> StageResult:
        provider = ai_providers.get_provider("tampering")
        # Forensics prefer the ORIGINAL upload: preprocessing re-encodes the
        # image (denoise/CLAHE/resize), which would erase or fabricate the
        # very compression/noise signals the detectors look for. Fall back
        # to the preprocessed path when no original is available.
        image_path = ctx.get("original_path") or ctx.get("preprocessed_image_path")
        payload = provider.analyze(image_path)
        return _provider_result(self.name, payload, ("tampering",), ctx)


class FaceVerificationStage:
    name = "face_verification"
    requires = ("preprocessed_image_path",)
    provides = ("face_verification",)

    def run(self, ctx: PipelineContext) -> StageResult:
        provider = ai_providers.get_provider("face")
        # Face matching prefers the ORIGINAL upload: preprocessing re-encodes
        # the image (denoise/CLAHE), which softens fine facial texture and can
        # trip the sharpness gate. Fall back to the preprocessed path when no
        # original is available.
        document_image = ctx.get("original_path") or ctx.get("preprocessed_image_path")
        payload = provider.verify(
            document_image_path=document_image,
            probe_image_path=ctx.get("probe_image_path"),
        )
        return _provider_result(self.name, payload, ("face_verification",), ctx)


class RiskAssessmentStage:
    name = "risk_assessment"
    # requires=() is deliberate: risk fusion must ALWAYS run so a crashed
    # upstream module (e.g. face) degrades to an 'unavailable' risk signal
    # (+points, human review) instead of skipping the final verdict entirely.
    # Upstream errors/skips reach the engine via ctx 'stage_results'.
    requires: tuple[str, ...] = ()
    provides = ("risk",)

    def run(self, ctx: PipelineContext) -> StageResult:
        provider = ai_providers.get_provider("risk")
        payload = provider.score(
            {
                "ocr_fields": ctx.get("ocr_fields"),
                "validation": ctx.get("validation"),
                "tampering": ctx.get("tampering"),
                "face_verification": ctx.get("face_verification"),
                "doc_type_hint": ctx.get("doc_type_hint"),
                "doc_type_detected": ctx.get("doc_type_detected"),
                "pdf_pages": ctx.get("pdf_pages"),
            },
            stage_results=ctx.get("stage_results"),
        )
        return _provider_result(self.name, payload, ("risk",), ctx)


STAGE_ORDER: list[type] = [
    PreprocessStage,
    OcrStage,
    DocumentValidationStage,
    TamperingStage,
    FaceVerificationStage,
    RiskAssessmentStage,
]
