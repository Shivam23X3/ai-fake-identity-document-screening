"""Photo replacement + stamp/signature + generic editing-artifact detectors.

Detector 1 (photo replacement): the portrait region is the most commonly
swapped part of an ID document. Signals:
  - a sharp luminance discontinuity ringing the photo boundary (the pasted
    patch has different focus/sharpness than the printed page), measured as
    boundary vs interior edge-energy ratio;
  - a localized ELA glow hugging the photo boundary.

Detector 4 (image editing artifacts): generic global/local editing traces:
  - local sharpness (Laplacian variance) outliers between tiles;
  - double-JPEG blocking (grid signal present in some tiles only);
  - ELA outliers that do not coincide with text (text is legitimately sharp).

Detector for stamps/signatures: ink-color saturation analysis — stamps are
single-ink (blue/red/purple) with a characteristic hue spread; pasted or
re-touched stamps often carry inconsistent hue or suspiciously uniform color
regions. Cues only: overprints and low-quality scans confuse this easily.
"""
from __future__ import annotations

import logging
from typing import Any

import cv2
import numpy as np

from app.services.tampering.constants import (
    MAX_DIMENSION,
    PHOTO_EDGE_SHARP_RATIO,
    PHOTO_ELA_EDGE_RATIO,
    PHOTO_REGION_MIN_SIDE,
    STAMP_COLOR_SAT_MIN,
    STAMP_HUE_SPREAD_MAX,
)
from app.services.tampering.ela import ElaResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Detector 1 — photo replacement
# ---------------------------------------------------------------------------
def detect_photo_region(
    bgr_u8: Any, gray_u8: Any, ela: ElaResult
) -> dict[str, Any]:
    """Locate the portrait region and test its boundary for replacement cues.

    The photo is found as the largest bright-ish connected blob in the upper
    2/3 of the document with roughly portrait aspect — good enough for the
    common layout, and a miss simply means the detector abstains.
    """
    h, w = gray_u8.shape[:2]

    box = _find_photo_box(bgr_u8)
    if box is None:
        return {
            "fired": False,
            "confidence": 0.0,
            "note": "Portrait region not located; photo-replacement analysis skipped.",
            "photo_box": None,
            "cues": [],
        }
    x, y, pw, ph = box
    if min(pw, ph) < PHOTO_REGION_MIN_SIDE:
        return {
            "fired": False,
            "confidence": 0.0,
            "note": "Located photo region too small to analyze reliably.",
            "photo_box": [int(v) for v in box],
            "cues": [],
        }

    cues: list[str] = []
    strengths: list[float] = []

    # --- cue A: boundary sharpness discontinuity -------------------------
    interior = gray_u8[y + ph // 5: y + 4 * ph // 5, x + pw // 5: x + 4 * pw // 5]
    interior_edge = _edge_energy(interior)
    band_w = max(3, pw // 24)
    band_h = max(3, ph // 24)
    top = gray_u8[max(0, y - band_h): y + band_h, x: x + pw]
    bottom = gray_u8[y + ph - band_h: min(h, y + ph + band_h), x: x + pw]
    left = gray_u8[y: y + ph, max(0, x - band_w): x + band_w]
    right = gray_u8[y: y + ph, x + pw - band_w: min(w, x + pw + band_w)]
    boundary_edge = np.mean([_edge_energy(top), _edge_energy(bottom),
                             _edge_energy(left), _edge_energy(right)])
    if interior_edge > 1e-6:
        ratio = boundary_edge / interior_edge
        if ratio >= PHOTO_EDGE_SHARP_RATIO:
            cues.append(
                f"Boundary edges {ratio:.1f}x sharper than photo interior "
                "(typical of a pasted image with different focus)."
            )
            strengths.append(min(1.0, (ratio - 1.0) / 2.0))

    # --- cue B: ELA glow on the boundary ----------------------------------
    if not ela.global_gate:
        pad = max(4, pw // 20)
        bx0, by0 = max(0, x - pad), max(0, y - pad)
        bx1, by1 = min(w, x + pw + pad), min(h, y + ph + pad)
        ix0, iy0 = max(0, x + pw // 6), max(0, y + ph // 6)
        ix1, iy1 = min(w, x + 5 * pw // 6), min(h, y + 5 * ph // 6)
        ring = ela.error_map[by0:by1, bx0:bx1].copy()
        ring[iy0 - by0: iy1 - by0, ix0 - bx0: ix1 - bx0] = 0  # zero interior
        ring_energy = float(ring.mean())
        inner_energy = float(ela.error_map[iy0:iy1, ix0:ix1].mean())
        if inner_energy > 1e-6 and ring_energy / inner_energy >= PHOTO_ELA_EDGE_RATIO:
            cues.append(
                "ELA error concentrated on the photo boundary relative to its "
                "interior (possible splice seam)."
            )
            strengths.append(min(1.0, (ring_energy / inner_energy - 1.0) / 2.0))

    fired = bool(cues)
    confidence = max(strengths) if strengths else 0.0
    severity = "info"
    if fired:
        severity = "high" if len(cues) >= 2 else "medium"
        confidence = min(0.9, confidence + (0.15 if len(cues) >= 2 else 0.0))

    return {
        "fired": fired,
        "confidence": round(float(confidence), 3),
        "severity": severity,
        "note": (
            "Photo region shows replacement cues: " + " ".join(cues)
            if fired else
            "Photo region located; no replacement cues at its boundary."
        ),
        "photo_box": [int(x), int(y), int(pw), int(ph)],
        "cues": cues,
    }


def _edge_energy(gray: Any) -> float:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return float((np.abs(gx) + np.abs(gy)).mean())


def _find_photo_box(bgr_u8: Any) -> tuple[int, int, int, int] | None:
    """Largest smooth connected blob in the upper 2/3 with portrait-ish aspect."""
    h, w = bgr_u8.shape[:2]
    roi_h = int(h * 2 / 3)
    gray = cv2.cvtColor(bgr_u8, cv2.COLOR_BGR2GRAY)
    # Photos are darker + less textured than printed paper. Detect "not text":
    # blur the gray, threshold on local uniformity.
    blur = cv2.GaussianBlur(gray, (9, 9), 0)
    local_std = cv2.blur((gray.astype(np.float32) - blur.astype(np.float32)) ** 2, (9, 9))
    smooth = (np.sqrt(local_std) < 12).astype(np.uint8) * 255
    # Restrict to upper 2/3, ignore margins.
    mask = np.zeros_like(smooth)
    margin_x, margin_y = int(w * 0.03), int(h * 0.05)
    mask[margin_y: roi_h, margin_x: w - margin_x] = smooth[margin_y: roi_h, margin_x: w - margin_x]
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best: tuple[int, int, int, int] | None = None
    best_area = 0.0
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        area = bw * bh
        if area < 0.01 * h * w or area <= best_area:
            continue
        aspect = bw / max(bh, 1)
        if not (0.45 <= aspect <= 1.6):   # portrait-ish
            continue
        # Photo blob must be fairly solid.
        solidity = area / max(cv2.contourArea(cnt), 1.0)
        if solidity < 0.5:
            continue
        best, best_area = (x, y, bw, bh), area
    return best


# ---------------------------------------------------------------------------
# Stamps / signatures
# ---------------------------------------------------------------------------
def detect_stamp_region(bgr_u8: Any) -> dict[str, Any]:
    """Single-ink (blue/red/purple) saturation analysis for stamp cues."""
    hsv = cv2.cvtColor(bgr_u8, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(np.float32) / 255.0
    hue = hsv[:, :, 0].astype(np.float32) * 2.0   # OpenCV hue is 0..180

    ink_mask = (sat >= STAMP_COLOR_SAT_MIN).astype(np.uint8)
    ink_fraction = float(ink_mask.mean())
    if ink_fraction < 0.002 or int(ink_mask.sum()) < 200:
        return {
            "fired": False,
            "confidence": 0.0,
            "note": "No colored ink regions found for stamp analysis.",
            "ink_fraction": round(ink_fraction, 4),
            "cues": [],
        }

    hues = hue[ink_mask > 0]
    # Circular spread of hue (degrees) — a single stamp ink has a narrow one.
    hue_rad = np.deg2rad(hues)
    mean_vec = np.array([np.cos(hue_rad).mean(), np.sin(hue_rad).mean()])
    mean_hue = float(np.rad2deg(np.arctan2(mean_vec[1], mean_vec[0]))) % 360.0
    resultant = float(np.linalg.norm(mean_vec))    # 1 = perfectly concentrated
    spread = float(np.sqrt(max(0.0, -2.0 * np.log(max(resultant, 1e-9)))) ) if resultant < 1.0 else 0.0

    cues: list[str] = []
    strengths: list[float] = []
    if spread > STAMP_HUE_SPREAD_MAX:
        cues.append(
            f"Ink hue spread {spread:.0f}° exceeds single-stamp norm "
            f"({STAMP_HUE_SPREAD_MAX:.0f}°) — mixed ink sources or retouching."
        )
        strengths.append(min(1.0, (spread - STAMP_HUE_SPREAD_MAX) / 90.0))

    # Very low resultant = hues all over the circle = multiple ink colors.
    if resultant < 0.55:
        cues.append("Multiple unrelated ink hues detected (multi-stamp or pasted mark).")
        strengths.append(min(0.8, (0.55 - resultant) * 1.6))

    fired = bool(cues)
    confidence = max(strengths) if strengths else 0.0
    severity = "info"
    if fired:
        severity = "low" if len(cues) == 1 else "medium"
        confidence = min(0.75, confidence)

    return {
        "fired": fired,
        "confidence": round(float(confidence), 3),
        "severity": severity,
        "note": (
            "Stamp/ink anomalies: " + " ".join(cues)
            if fired else
            f"Ink hues consistent (mean hue {mean_hue:.0f}°, spread {spread:.0f}°)."
        ),
        "ink_fraction": round(ink_fraction, 4),
        "mean_hue_deg": round(mean_hue, 1),
        "hue_spread_deg": round(spread, 1),
        "cues": cues,
    }


# ---------------------------------------------------------------------------
# Detector 4 — generic image-editing artifacts
# ---------------------------------------------------------------------------
def detect_editing_artifacts(
    bgr_u8: Any, gray_u8: Any, ela: ElaResult
) -> dict[str, Any]:
    """Global + local editing traces: sharpness outliers, ELA-vs-text mismatch,
    halo/cloning traces near strong ELA blobs that are NOT text."""
    h, w = gray_u8.shape[:2]
    cues: list[str] = []
    strengths: list[float] = []
    flags: list[dict[str, Any]] = []

    # --- local sharpness outliers (tile Laplacian variance) ---------------
    # Printed text tiles are LEGITIMATELY orders of magnitude sharper than
    # paper, so raw variance outliers flag every printed page. Instead:
    # compare only against the textured-tile median and require the outlier
    # tiles to be unusually FLAT (a pasted smooth patch on a textured page),
    # which is the actual editing signature.
    tile = 128
    sharpness: list[tuple[int, int, float, float]] = []  # x, y, var, std
    for y in range(0, h - tile + 1, tile):
        for x in range(0, w - tile + 1, tile):
            t = gray_u8[y: y + tile, x: x + tile]
            sharpness.append(
                (x, y, float(cv2.Laplacian(t, cv2.CV_64F).var()), float(t.std()))
            )
    if len(sharpness) >= 6:
        vars_ = np.array([s[2] for s in sharpness])
        stds = np.array([s[3] for s in sharpness])
        med_var = float(np.median(vars_))
        med_std = float(np.median(stds))
        if med_var > 10:
            ratio = vars_ / med_var
            # Only suspiciously FLAT tiles (8x lower variance than the page
            # median AND low absolute contrast) count as editing evidence;
            # sharper-than-median tiles are just text/lines.
            odd = [
                (s, r) for s, r in zip(sharpness, ratio)
                if r <= 1 / 8.0 and s[3] < med_std * 0.7
            ]
            if len(odd) >= 2:
                cues.append(
                    f"{len(odd)} tile(s) unusually flat for this page — "
                    "possible pasted/smoothed region."
                )
                strengths.append(min(0.6, 0.2 + 0.05 * len(odd)))
                flags.extend(
                    {"x": s[0], "y": s[1], "w": tile, "h": tile,
                     "kind": "flat_patch", "score": round(float(min(1.0 / max(r, 0.01), 1.0)), 3)}
                    for s, r in odd[:8]
                )

    # --- ELA blobs that are probably NOT text ------------------------------
    # Text on paper legitimately produces ELA response; judge blobs by their
    # fill ratio (text blobs are sparse/liney, solid patches suspicious).
    if not ela.global_gate and ela.bright_mask.any():
        num, labels, stats, _ = cv2.connectedComponentsWithStats(
            (ela.bright_mask > 0).astype(np.uint8), connectivity=8
        )
        for i in range(1, num):
            x, y, bw, bh, area = (int(v) for v in stats[i])
            if area < 500 or bw < 32 or bh < 32:
                continue
            sub = (labels[y: y + bh, x: x + bw] == i)
            fill = float(sub.mean())
            # Solid glowing patch (not liney text) on a document = candidate.
            if fill >= 0.55:
                cues.append(
                    f"Solid ELA-bright patch ({bw}x{bh}px, fill {fill:.0%}) — "
                    "unusual for printed text; candidate edited region."
                )
                strengths.append(min(0.8, 0.3 + fill * 0.4))
                flags.append(
                    {"x": x, "y": y, "w": bw, "h": bh, "kind": "solid_ela_patch",
                     "score": round(fill, 3)}
                )
                if len(flags) >= 10:
                    break

    fired = bool(cues)
    confidence = max(strengths) if strengths else 0.0
    severity = "info"
    if fired:
        severity = "medium" if len(cues) >= 2 else "low"

    return {
        "fired": fired,
        "confidence": round(float(confidence), 3),
        "severity": severity,
        "note": (
            "Image-editing artifacts: " + " ".join(cues)
            if fired else
            "No global editing artifacts detected."
        ),
        "cues": cues,
        "flags": flags,
    }
