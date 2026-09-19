"""Image-quality safeguards for face verification.

Every check returns a :class:`QualityIssue` (or ``None``) so callers can
aggregate reasons into the response. Checks are deliberately conservative:
they exist to route unusable images to INCONCLUSIVE + human review, never
to silently guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from app.services.face.constants import (
    BLUR_LAPLACIAN_MIN,
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    FLATNESS_STD_MIN,
    OVEREXPOSURE_MAX_FRACTION,
)


@dataclass
class QualityIssue:
    """One quality problem that degrades (or blocks) verification."""

    code: str          # e.g. "too_blurry"
    note: str          # human-readable explanation for the UI
    severity: str      # "blocking" (cannot verify) | "degrading" (reduces confidence)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "note": self.note, "severity": self.severity}


def _crop(bgr: Any, box: dict[str, int] | None) -> Any:
    if not box:
        return bgr
    h, w = bgr.shape[:2]
    x0 = max(0, int(box.get("x", 0)))
    y0 = max(0, int(box.get("y", 0)))
    x1 = min(w, x0 + max(1, int(box.get("w", w))))
    y1 = min(h, y0 + max(1, int(box.get("h", h))))
    if x1 <= x0 or y1 <= y0:
        return bgr
    return bgr[y0:y1, x0:x1]


def check_blur(bgr: Any, box: dict[str, int] | None = None) -> QualityIssue | None:
    """Face crop variance-of-Laplacian below threshold ⇒ too blurry."""
    crop = _crop(bgr, box)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if score < BLUR_LAPLACIAN_MIN:
        return QualityIssue(
            code="too_blurry",
            note=f"Image too blurry for reliable face matching (sharpness {score:.0f} < {BLUR_LAPLACIAN_MIN:.0f}).",
            severity="blocking",
        )
    return None


def check_lighting(bgr: Any, box: dict[str, int] | None = None) -> QualityIssue | None:
    """Mean luminance outside the usable window ⇒ poor lighting."""
    crop = _crop(bgr, box)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mean = float(gray.mean())
    if mean < BRIGHTNESS_MIN:
        return QualityIssue(
            code="too_dark",
            note=f"Image too dark for reliable face matching (mean luminance {mean:.0f}).",
            severity="blocking",
        )
    if mean > BRIGHTNESS_MAX:
        return QualityIssue(
            code="too_bright",
            note=f"Image overexposed for reliable face matching (mean luminance {mean:.0f}).",
            severity="blocking",
        )
    blown = float((gray >= 245).mean())
    if blown > OVEREXPOSURE_MAX_FRACTION:
        return QualityIssue(
            code="overexposed_regions",
            note=f"{blown:.0%} of the face area is blown out; matching reliability is reduced.",
            severity="degrading",
        )
    return None


def check_flatness(bgr: Any, box: dict[str, int] | None = None) -> QualityIssue | None:
    """Featureless (near-constant) face crop ⇒ nothing to embed."""
    crop = _crop(bgr, box)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    std = float(gray.std())
    if std < FLATNESS_STD_MIN:
        return QualityIssue(
            code="featureless_region",
            note="Face region is nearly featureless (flat patch); cannot verify.",
            severity="blocking",
        )
    return None


def evaluate_quality(bgr: Any, box: dict[str, int] | None = None) -> list[QualityIssue]:
    """Run all quality checks; return the issues found (possibly empty)."""
    issues: list[QualityIssue] = []
    for check in (check_blur, check_lighting, check_flatness):
        try:
            issue = check(bgr, box)
        except Exception:  # noqa: BLE001 - quality checks must never crash the stage
            continue
        if issue is not None:
            issues.append(issue)
    return issues
