"""Risk-assessment module (deterministic weighted rules fusion, Step 8).

Self-contained sub-package: signal extraction from upstream stage outputs
(OCR quality, validation, tampering, face, expiry, missing fields,
suspicious patterns, metadata anomalies) → weighted fusion on a 0–100
scale → configurable LOW/MEDIUM/HIGH/CRITICAL level + explainable reasons.

The pipeline reaches it only through the ``RiskScoringProvider`` interface
in ``app/services/ai_providers.py``; everything here is usable standalone
(see ``engine.assess_risk``).

Honesty policy: the score is advisory, fully explainable (``reasons[]`` +
``contributions[]``), band thresholds are configurable policy rather than
claimed facts, and routing can only ever require MORE human attention,
never less. No automatic accept/reject exists.
"""
from app.services.risk.constants import (
    LEVELS,
    LEVEL_CRITICAL,
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
)
from app.services.risk.engine import RiskAssessmentError, assess_risk

__all__ = [
    "LEVELS",
    "LEVEL_CRITICAL",
    "LEVEL_HIGH",
    "LEVEL_LOW",
    "LEVEL_MEDIUM",
    "RiskAssessmentError",
    "assess_risk",
]
