"""Compression inconsistency (detector 7): JPEG 8x8 grid coherence.

Original JPEGs have block-boundary discontinuities aligned to the 8x8 DCT
grid. A region pasted from a differently-compressed source breaks that
alignment: its blockiness is weaker or offset relative to the rest of the
image. We measure per-tile grid-aligned vs grid-crossing gradient energy and
look for tiles that deviate from the image median.

Honesty: strong text edges, rescaling and PNG/WEBP sources (no 8x8 grid at
all) produce weak or meaningless grid signals — the detector self-scores
confidence accordingly and abstains on non-JPEG input.
"""
from __future__ import annotations

import logging
from typing import Any

import cv2
import numpy as np

from app.services.tampering.constants import JPEG_GRID_TILE

logger = logging.getLogger(__name__)


def _grid_measures(gray_u8: Any) -> tuple[float, float] | None:
    """Return (grid-aligned energy, crossing energy) for one tile.

    Grid-aligned: mean |gradient| along columns x ≡ 0 (mod 8) and rows
    y ≡ 0 (mod 8). Crossing: the same measure over mid-block positions.
    The ratio aligned/crossing is the classic blockiness signature.
    """
    gx = np.abs(cv2.Sobel(gray_u8, cv2.CV_32F, 1, 0, ksize=3)).sum(axis=0)  # per column
    gy = np.abs(cv2.Sobel(gray_u8, cv2.CV_32F, 0, 1, ksize=3)).sum(axis=1)  # per row
    h, w = gx.shape[0], gy.shape[0]

    def _aligned(v: Any, offset: int) -> float:
        idx = np.arange(offset, len(v), 8)
        return float(v[idx].mean()) if len(idx) else 0.0

    def _crossing(v: Any, offset: int) -> float:
        idx = np.arange(offset + 4, len(v), 8)
        return float(v[idx].mean()) if len(idx) else 0.0

    # Try all 8 phases and take the strongest alignment (grids may be offset
    # by crop/resize); report the best phase's aligned/crossing pair.
    best: tuple[float, float] | None = None
    for phase in range(8):
        a_col, c_col = _aligned(gx, phase), _crossing(gx, phase)
        a_row, c_row = _aligned(gy, phase), _crossing(gy, phase)
        aligned = (a_col + a_row) / 2.0
        crossing = (c_col + c_row) / 2.0
        if crossing <= 0:
            continue
        ratio = aligned / crossing
        if best is None or ratio > best[0] / max(best[1], 1e-6):
            best = (aligned, crossing)
    if best is None or best[1] <= 0:
        return None
    return best


def analyze_compression(
    gray_u8: Any,
    *,
    is_jpeg: bool,
    tile: int = JPEG_GRID_TILE,
    z_threshold: float = 3.0,
) -> dict[str, Any]:
    """JPEG grid-coherence analysis. Abstains on non-JPEG input."""
    if not is_jpeg:
        return {
            "fired": False,
            "confidence": 0.0,
            "note": "Not a JPEG; 8x8 grid analysis not applicable.",
            "tiles": [], "outliers": [],
        }

    h, w = gray_u8.shape[:2]
    measures: list[dict[str, Any]] = []
    ys = list(range(0, h, tile))
    xs = list(range(0, w, tile))
    for y in ys:
        for x in xs:
            tw = min(tile, w - x)
            th = min(tile, h - y)
            if tw < 64 or th < 64:
                continue
            t = gray_u8[y: y + th, x: x + tw]
            m = _grid_measures(t)
            if m is None:
                continue
            aligned, crossing = m
            measures.append(
                {"x": x, "y": y, "ratio": aligned / max(crossing, 1e-6)}
            )

    if len(measures) < 6:
        return {
            "fired": False,
            "confidence": 0.0,
            "note": "Too few analyzable tiles for grid coherence.",
            "tiles": measures, "outliers": [],
        }

    ratios = np.array([m["ratio"] for m in measures], dtype=np.float32)
    med = float(np.median(ratios))
    mad = float(np.median(np.abs(ratios - med)))
    spread = max(1.4826 * mad, 0.05)

    outliers = []
    for m in measures:
        z = (m["ratio"] - med) / spread
        if abs(z) >= z_threshold:
            outliers.append({**m, "z": round(z, 2)})

    fired = len(outliers) >= 2
    confidence = 0.0
    severity = "info"
    if fired:
        # Grid signals are weak on busy documents; keep confidence capped.
        confidence = min(0.6, 0.25 + 0.07 * len(outliers))
        severity = "medium" if len(outliers) >= 5 else "low"

    return {
        "fired": fired,
        "confidence": round(confidence, 3),
        "severity": severity,
        "note": (
            f"{len(outliers)} tile(s) with 8x8 block-grid coherence deviating "
            f"from the image median (median ratio {med:.2f})."
            if fired else
            f"8x8 grid coherence consistent across tiles (median ratio {med:.2f})."
        ),
        "tiles": measures,
        "outliers": outliers,
        "median_ratio": round(med, 3),
    }
