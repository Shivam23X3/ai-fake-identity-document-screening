"""Request validation DTOs for screening endpoints."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

ALLOWED_DOC_TYPE_HINTS = {
    "passport",
    "visa",
    "national_id",
    "driving_license",
    "permit",
    "unknown",
}


class AnalyzeRequest(BaseModel):
    """Body of POST /api/screening/analyze."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=8, max_length=64, pattern=r"^[a-f0-9]+$")
    doc_type_hint: str = Field(default="unknown")


class ScreeningSummary(BaseModel):
    """Row shape returned by history/detail lists."""

    run_id: str
    doc_type_hint: str | None
    doc_type_detected: str | None
    status: str
    risk_score: float | None
    risk_band: str | None
    human_review_required: bool
    created_at: str | None


class AnalyzeResponse(BaseModel):
    run_id: str
    status: str
    doc_type_hint: str
    doc_type_detected: str | None
    risk_score: float | None
    risk_band: str | None
    human_review_required: bool
    stages: list[dict]
