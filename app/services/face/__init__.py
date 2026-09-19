"""Face-verification module (Step 7) — one-to-one verification ONLY.

This module compares the portrait on a presented document with a live
capture of the person presenting it (1:1 verification). It NEVER performs
biometric identification (1:N search against a population database) — that
capability is deliberately absent.
"""
from app.services.face.constants import (
    MATCH,
    NO_MATCH,
    VERDICT_INCONCLUSIVE,
    VERDICTS,
)

__all__ = [
    "MATCH",
    "NO_MATCH",
    "VERDICT_INCONCLUSIVE",
    "VERDICTS",
]
