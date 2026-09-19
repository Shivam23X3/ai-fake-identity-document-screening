"""Noise inconsistency (detector 3): PRNU-style heuristic via local variance.

Genuinely uniform captures (one sensor, one lighting pass) have approximately
stationary local noise statistics. Regions pasted from other sources usually
carry different noise levels. We estimate local noise as high-frequency energy
(Laplacian-based) and compare tile statistics against the global median.

Honesty: flat paper + aggressive in-camera noise reduction, heavy compression
and rescaling all confuse local-variance estimators — a mismatch is a lead,
never proof. Confidence is scaled by coverage (usable tiles / total tiles).
"""
from __future__ import annotations

import logging
from typing import Any

import cv2
import numpy as np

from app.services.tampering.constants import NOISE_TILE

logger = logging.getLogger(__name__)


def analyze_noise(
    gray_f: Any,
    *,
    noise_tile: int = NOISE_TILE,
    ratio_hard: float = 2.0,
    ratio_soft: float = 1.5,
    min_usable_fraction: float = 0.3,
) -> dict[str, Any]:
    """Local-variance noise analysis over tiles of a float32 grayscale image.

    Returns a payload with tile stats, outlier flags and a confidence that
    reflects how much of the image was actually usable (enough texture).
    """
    h, w = gray_f.shape[:2]
    tiles: list[dict[str, Any]] = []
    outliers: list[dict[str, Any]] = []

    ys = list(range(0, h, noise_tile))
    xs = list(range(0, w, noise_tile))
    for y in ys:
        for x in xs:
            tw = min(noise_tile, w - x)
            th = min(noise_tile, h - y)
            if tw < noise_tile // 2 or th < noise_tile // 2:
                continue  # ignore edge slivers
            tile = gray_f[y: y + th, x: x + tw]
            # High-frequency energy: MEDIAN |Laplacian| after slight blur.
            # The median (not the mean) is what lets document pages work:
            # a printed text line crosses few pixels of a tile, so the
            # median stays at paper-noise level, while a pasted noisy patch
            # raises the whole tile's distribution. A fixed floor would
            # classify all of a flat document as 'unusable'.
            blurred = cv2.GaussianBlur(tile, (3, 3), 0)
            hf = float(np.median(np.abs(cv2.Laplacian(blurred, cv2.CV_32F))))
            tiles.append({"x": x, "y": y, "noise": round(hf, 3)})

    if not tiles:
        return {
            "fired": False,
            "confidence": 0.0,
            "note": "Image too small/uniform for noise analysis.",
            "tiles": [], "outliers": [],
            "global_median_noise": None,
            "usable_fraction": 0.0,
        }

    noise_values = np.array([t["noise"] for t in tiles], dtype=np.float32)
    med = float(np.median(noise_values))
    mad = float(np.median(np.abs(noise_values - med)))  # robust spread

    # 'Usable' = tile noise is measurable relative to the page median (a
    # sensor/scan noise floor always leaves SOME high-frequency energy; a
    # purely digital blank has none and the analysis abstains honestly).
    usable_floor = max(1e-4, 0.25 * med)
    usable = sum(1 for t in tiles if t["noise"] > usable_floor)

    for t in tiles:
        if t["noise"] <= usable_floor:
            continue
        # Robust z-score relative to the median absolute deviation.
        z = (t["noise"] - med) / max(1.4826 * mad, 1e-6)
        if z >= 4.0 or (mad < 1e-6 and t["noise"] > med * 8):
            outliers.append({"x": t["x"], "y": t["y"], "z": round(z, 2),
                             "noise": t["noise"], "ratio": round(t["noise"] / max(med, 1e-6), 2)})

    usable_fraction = usable / max(len(tiles), 1)
    n_out = len(outliers)
    fired = False
    severity = "info"
    confidence = 0.0

    if n_out >= 4 and usable_fraction >= min_usable_fraction:
        fired = True
        # Confidence scales with how many tiles disagree and how usable the
        # image is; capped so single tiles cannot drive it to 1.0.
        confidence = min(0.75, 0.25 + 0.08 * n_out) * min(1.0, usable_fraction * 1.6)
        severity = "medium" if n_out >= 8 else "low"

    return {
        "fired": fired,
        "confidence": round(confidence, 3),
        "severity": severity,
        "note": (
            f"{n_out} tile(s) with noise statistics far from the image median "
            f"(usable tiles: {usable}/{len(tiles)})."
            if fired else
            f"No significant local noise outliers (usable tiles: {usable}/{len(tiles)})."
        ),
        "tiles": tiles,
        "outliers": outliers,
        "global_median_noise": round(med, 4),
        "usable_fraction": round(usable_fraction, 3),
    }
