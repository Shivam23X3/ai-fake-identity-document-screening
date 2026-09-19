"""Pipeline assembly for the document screening flow.

To plug in, reorder or replace stages later, edit
``build_document_screening_pipeline``. No other file needs to change.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.pipeline import Pipeline
from app.pipelines.stages import (
    DocumentValidationStage,
    FaceVerificationStage,
    OcrStage,
    PreprocessStage,
    RiskAssessmentStage,
    TamperingStage,
)


def build_document_screening_pipeline() -> Pipeline:
    settings = get_settings()
    return Pipeline(
        name="document_screening",
        stages=[
            PreprocessStage(),
            OcrStage(),
            DocumentValidationStage(),
            TamperingStage(),
            FaceVerificationStage(),
            RiskAssessmentStage(),
        ],
        failure_policy=settings.stage_failure_policy,
    )
