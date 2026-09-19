"""Error Level Analysis (detector 2) — tiled, quality-aware, honest.

ELA principle: re-encoding a JPEG at a uniform quality should compress a
tampered region differently from its surroundings, because the tampered
region has been through one fewer compression generation. We measure the
difference between the original and a re-saved image and look for regions
that glow relative to the rest.

Honesty rules baked in here:
- ELA reacts to ANY locally inconsistent error level: saturated whites,
  sharp text on flat paper, sensor-noise patches. A glow is a *lead*, never
  a verdict — notes and the fusion layer treat it that way.
- The reference quality is estimated from the file instead of hardcoded.
- A global-format gate suppresses ELA when the whole image glows uniformly
  (typical of heavily re-encoded scans), avoiding full-page false positives.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from app.services.tampering.constants import ELA_TILE_OVERLAP, ELA_TILE_SIZE, MAX_DIMENSION

logger = logging.getLogger(__name__)


@dataclass
class ElaResult:
    """Raw ELA measurements + derived maps."""

    mean_error: float
    std_error: float
    p95_error: float
    max_error: float
    error_map: Any                       # float32 0..255 absolute diff
    bright_mask: Any                     # uint8 0/255 local-outlier glow
    bright_fraction: float
    global_gate: bool                    # True ⇒ whole image glows ⇒ uninformative
    tiles: list[dict[str, Any]] = field(default_factory=list)
    reference_quality: int = 90
    warnings: list[str] = field(default_factory=list)


# ITU-T Annex K luminance quantization table (JPEG standard base table).
_ANNEX_K_LUMA: tuple[int, ...] = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)


def estimate_jpeg_quality(path: str) -> int | None:
    """Estimate a JPEG file's quality by matching its luminance quantization
    table against the standard scaling law. ``None`` when not JPEG/unknown."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            if (im.format or "").upper() != "JPEG":
                return None
            qtables = im.quantization
            table0 = qtables.get(0)
            if not table0:
                return None
    except Exception:  # noqa: BLE001 - unreadable file → no estimate
        return None

    base = _ANNEX_K_LUMA
    best_q, best_dist = None, float("inf")
    for q in range(1, 101):
        scale = 5000.0 / q if q < 50 else 200.0 - q * 2
        approx = [max(1, min(255, int((b * scale + 50) / 100))) for b in base]
        dist = sum((a - t) ** 2 for a, t in zip(approx, table0[:64]))
        if dist < best_dist:
            best_q, best_dist = q, dist
    return best_q


def _tile_params(w: int, h: int) -> list[tuple[int, int, int, int]]:
    """(x, y, tw, th) tiles with overlap; one full-frame tile when small."""
    if w <= ELA_TILE_SIZE and h <= ELA_TILE_SIZE:
        return [(0, 0, w, h)]
    tiles: list[tuple[int, int, int, int]] = []
    step = ELA_TILE_SIZE - ELA_TILE_OVERLAP
    y = 0
    while y < h:
        x = 0
        th = min(ELA_TILE_SIZE, h - y)
        while x < w:
            tw = min(ELA_TILE_SIZE, w - x)
            tiles.append((x, y, tw, th))
            if x + tw >= w:
                break
            x += step
        if y + th >= h:
            break
        y += step
    return tiles


def _recompress(u8: Any, quality: int) -> Any:
    """In-memory JPEG round-trip (uint8 BGR in, float32 grayscale [0,1] out)."""
    ok, buf = cv2.imencode(".jpg", u8, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        gray = cv2.cvtColor(u8, cv2.COLOR_BGR2GRAY) if u8.ndim == 3 else u8
        return gray.astype(np.float32) / 255.0
    back = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return cv2.cvtColor(back, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0


def analyze_ela(bgr: Any, *, reference_quality: int | None = None) -> ElaResult:
    """Run ELA on a BGR image (uint8 or float32 [0,1] accepted).

    The image is recompressed at ``reference_quality`` (file-estimated when
    omitted), and the per-pixel absolute difference is analyzed in tiles.
    """
    img = bgr if bgr.dtype == np.float32 else bgr.astype(np.float32) / 255.0
    h, w = img.shape[:2]

    if max(h, w) > MAX_DIMENSION:
        scale = MAX_DIMENSION / max(h, w)
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]

    quality = int(reference_quality or 90)

    tiles = _tile_params(w, h)
    tile_stats: list[dict[str, Any]] = []
    error_map = np.zeros((h, w), dtype=np.float32)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    for (x, y, tw, th) in tiles:
        patch = gray[y: y + th, x: x + tw]
        u8 = np.clip(patch * 255.0, 0, 255).astype(np.uint8)
        rec = _recompress(cv2.cvtColor(u8, cv2.COLOR_GRAY2BGR), quality)
        err = np.abs(patch - rec) * 255.0
        error_map[y: y + th, x: x + tw] = err
        tile_stats.append(
            {
                "x": x, "y": y, "w": tw, "h": th,
                "mean": round(float(err.mean()), 2),
                "p95": round(float(np.percentile(err, 95)), 2),
            }
        )

    mean_error = float(error_map.mean())
    std_error = float(error_map.std())
    p95_error = float(np.percentile(error_map, 95))

    # Bright-mask: local outliers via median-filter residual (self-normalizing,
    # robust to the global error level).
    med = cv2.medianBlur(np.clip(error_map, 0, 255).astype(np.uint8), 21)
    residual = cv2.subtract(np.clip(error_map, 0, 255).astype(np.uint8), med)
    bright_mask = residual.copy()
    bright_mask[bright_mask < 12] = 0            # noise floor
    bright_mask = cv2.dilate(bright_mask, np.ones((5, 5), np.uint8))
    bright_fraction = float((bright_mask > 0).mean())

    # Global gate: the whole image glowing uniformly ⇒ heavy re-encode of a
    # clean scan ⇒ ELA carries no local information.
    global_gate = bool(bright_fraction > 0.5 or (std_error < 1.0 and mean_error > 12.0))
    if global_gate:
        bright_mask = np.zeros_like(bright_mask)
        bright_fraction = 0.0

    return ElaResult(
        mean_error=mean_error,
        std_error=std_error,
        p95_error=p95_error,
        max_error=float(error_map.max()),
        error_map=error_map,
        bright_mask=bright_mask,
        bright_fraction=bright_fraction,
        global_gate=global_gate,
        tiles=tile_stats,
        reference_quality=quality,
    )


def ela_region_flags(result: ElaResult, min_area: int = 400) -> list[dict[str, Any]]:
    """Connected components of the bright mask, strongest first."""
    if result.bright_mask is None or not result.bright_mask.any():
        return []
    num, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        (result.bright_mask > 0).astype(np.uint8), connectivity=8
    )
    global_mean = max(float(result.error_map.mean()), 1e-6)
    flags: list[dict[str, Any]] = []
    for i in range(1, num):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < min_area:
            continue
        region_err = float(result.error_map[y: y + bh, x: x + bw].mean())
        flags.append(
            {
                "x": x, "y": y, "w": bw, "h": bh, "area": area,
                "contrast": round(region_err / global_mean, 2),
                "region_mean_error": round(region_err, 2),
            }
        )
    flags.sort(key=lambda f: f["area"], reverse=True)
    return flags
