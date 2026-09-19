"""Copy-paste / copy-move detection (detector 6).

Three complementary shift-HYPOTHESIS generators (real forgeries paste at
arbitrary offsets, so no single method is enough):

1. ORB keypoint matching — offset-invariant; finds structured duplicated
   content (stamps, photos, text blocks) at ANY paste offset.
2. DCT block lattice (all 8 phases) — catches unstructured texture
   duplication when the paste offset happens to be lattice-aligned.
3. FFT autocorrelation peaks — enumerates promising offsets DIRECTLY from
   the whole-image correlation surface, covering arbitrary paste offsets
   and keypoint-free texture (a noise patch has no corners to match).

Every hypothesis is then verified on the PIXELS at the exact offset:
- flat content carries no evidence (texture floor / keypoint response)
- near-neighbor matches (within 2 block widths) are excluded
- repeated layouts (text lines, guilloche) produce MANY collinear shift
  clusters at multiples of the layout pitch; a family whose accumulated
  pair count along one base direction reaches the cluster threshold is
  suppressed as periodic
- the agreeing textured region must form a compact, DENSE connected
  component (fill ratio ≥ ~0.45 of its bounding box, ≥24px in both
  dimensions). Text/pattern self-similarity scatters page-wide in thin
  stripes (fill ≈ 0.1–0.3, height ≈ one text line) and is rejected.

Even when it fires, the indicator means "consistent with a duplicated
region", not "proven copy-move" — severity is capped and fusion stays
cautious.
"""
from __future__ import annotations

import logging
from collections import Counter
from math import gcd
from typing import Any

import cv2
import numpy as np

from app.services.tampering.constants import (
    COPYMOVE_BLOCK,
    COPYMOVE_CLUSTER_MIN_PAIRS,
    COPYMOVE_MAX_DIM,
    COPYMOVE_MAX_PAIRS,
    COPYMOVE_MIN_PAIR_DISTANCE,
    COPYMOVE_STRIDE,
    COPYMOVE_TEXTURE_FLOOR,
    COPYMOVE_TOPK,
)

logger = logging.getLogger(__name__)

_SIM_THRESHOLD = 0.98     # DCT cosine similarity for candidate block pairs
_NCC_THRESHOLD = 0.90     # refined normalized cross-correlation acceptance
_NCC_WINDOW = 3           # px of refinement search around the candidate
_SHIFT_TOLERANCE = 4      # px buckets for shift clustering
_ORB_FEATURES = 3000
_ORB_MAX_MATCH_DISTANCE = 48  # hamming distance cap for descriptor matches

# Pixel-level shift verification.
_VERIFY_TOP_CLUSTERS = 20    # non-periodic clusters verified per analysis
_VERIFY_MIN_PAIRS = 3        # clusters smaller than this are never verified
_AGREE_DIFF = 0.05           # per-pixel agreement tolerance (float 0-1)
_MIN_AGREE_AREA = 900        # at least ~30x30 px of dense agreement
_MIN_AGREE_FRAC = 0.008      # agreement must cover ≥0.8% of the page
_MIN_SIDE_PX = 48            # dense blob must be ≥48px in BOTH dimensions.
                             # A text line is ~16px tall and stacked text
                             # rows form ribbons ~40px tall — neither can
                             # ever be a duplicated REGION.
_COMPACT_FILL_MIN = 0.45     # agreeing component must fill its bbox densely
_COMPACT_BBOX_MAX = 0.30     # ... and its bbox must stay under this page fraction

# FFT autocorrelation shift hypotheses.
_FFT_MIN_LAG = 16            # ignore autocorrelation peaks below this lag
_FFT_MIN_CORR = 0.12         # only peaks this strong become hypotheses
_FFT_NMS_RADIUS = 12         # min px between retained autocorrelation peaks
_FFT_MAX_PEAKS = 24          # max shift hypotheses from the FFT surface

# Independent pixel-tile support (honest pair_count semantics).
_TILE_SUPPORT_SIZE = 8       # px per supporting tile
_TILE_SUPPORT_MIN_FILL = 0.75  # fraction of a tile that must agree to count


def _dct_lowfreq_signature(block_f: Any, keep: int = 8) -> Any:
    """Low-frequency DCT signature of one block (robust to brightness),
    L2-normalized so plain brightness shifts don't match."""
    dct = cv2.dct(block_f)
    sig = dct[:keep, :keep].flatten()
    norm = float(np.linalg.norm(sig)) or 1.0
    return sig / norm


def _ncc_refine(
    gray_f: Any, x1: int, y1: int, x2: int, y2: int, block: int
) -> tuple[float, int, int] | None:
    """Refine a candidate pair with normalized cross-correlation over a
    ±_NCC_WINDOW search window around (x2, y2). Returns (ncc, dx, dy)."""
    h, w = gray_f.shape[:2]
    half = _NCC_WINDOW
    wx0, wy0 = x2 - half, y2 - half
    wx1, wy1 = x2 + block + half, y2 + block + half
    if wx0 < 0 or wy0 < 0 or wx1 > w or wy1 > h:
        return None
    tmpl = gray_f[y1: y1 + block, x1: x1 + block]
    window = gray_f[wy0: wy1, wx0: wx1]
    if tmpl.size == 0 or window.size == 0 or window.shape[0] < tmpl.shape[0] \
            or window.shape[1] < tmpl.shape[1]:
        return None
    res = cv2.matchTemplate(window, tmpl, cv2.TM_CCOEFF_NORMED)
    _minv, maxv, _lmin, lmax = cv2.minMaxLoc(res)
    dx = (wx0 + lmax[0]) - x1
    dy = (wy0 + lmax[1]) - y1
    return float(maxv), int(dx), int(dy)


def _orb_candidates(gray_u8: Any, min_dist: int) -> list[tuple[int, int, int, int]]:
    """(x1, y1, x2, y2) keypoint matches with displaced, self-consistent
    descriptors. Offset-invariant candidate source for structured content."""
    try:
        orb = cv2.ORB_create(nfeatures=_ORB_FEATURES, scaleFactor=1.2, nlevels=4)
        kps, desc = orb.detectAndCompute(gray_u8, None)
    except Exception:  # noqa: BLE001 - ORB misbehaving must not kill the stage
        return []
    if desc is None or kps is None or len(kps) < 20:
        return []
    try:
        bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        # k=3: the best match of a descriptor against its own set is itself;
        # in a copy-move forgery the duplicate can rank 2nd OR 3rd (other
        # keypoints may sit in between), so both non-self matches are taken
        # as candidates. NCC verification rejects the false ones.
        knn = bf.knnMatch(desc, desc, k=3)
    except Exception:  # noqa: BLE001
        return []

    pairs: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int]] = set()
    for matches in knn:
        if not matches or len(matches) < 2:
            continue
        for m in matches[1:]:   # skip self-match at matches[0]
            if m.distance > _ORB_MAX_MATCH_DISTANCE:
                continue
            i, j = m.queryIdx, m.trainIdx
            if i == j:
                continue
            p1 = kps[i].pt
            p2 = kps[j].pt
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            if dx * dx + dy * dy < min_dist * min_dist:
                continue
            key = (i, j) if i < j else (j, i)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((int(round(p1[0])), int(round(p1[1])),
                          int(round(p2[0])), int(round(p2[1]))))
    return pairs


def _dct_candidates(gray_f: Any, wh: int, ww: int) -> tuple[list[tuple[int, int, int, int]], int]:
    """(x1, y1, x2, y2) block pairs from an eight-phase DCT lattice.

    Real forgeries paste at ARBITRARY offsets; with a stride-8 lattice, a
    patch pasted at offset ≡ 2 mod 8 only has lattice-aligned source and
    destination blocks in the phase-2 lattice — misaligned blocks straddle
    the paste seam and never match. Each phase is a self-contained lattice
    (same phase mod 8 on both block positions is required for an exact
    shift match), so phases are processed round-robin with a per-phase
    candidate budget: every alignment contributes before the global
    COPYMOVE_MAX_PAIRS cap is reached."""
    block = COPYMOVE_BLOCK
    stride = COPYMOVE_STRIDE
    min_dist = COPYMOVE_MIN_PAIR_DISTANCE
    per_phase_budget = max(50, COPYMOVE_MAX_PAIRS // stride)
    candidates: list[tuple[int, int, int, int]] = []
    n_blocks = 0

    for phase in range(stride):  # 0..7 — every lattice alignment
        blocks: list[tuple[int, int, Any]] = []
        for y in range(phase, wh - block, stride):
            for x in range(phase, ww - block, stride):
                patch = gray_f[y: y + block, x: x + block]
                if float(patch.std() * 255.0) < COPYMOVE_TEXTURE_FLOOR:
                    continue
                blocks.append((x, y, _dct_lowfreq_signature(patch)))
        n_blocks += len(blocks)
        if len(blocks) < 30:
            continue
        sig = np.stack([b[2] for b in blocks])
        sims = sig @ sig.T
        phase_candidates: list[tuple[int, int, int, int]] = []
        for i in range(len(blocks)):
            if len(phase_candidates) >= per_phase_budget:
                break
            row = sims[i]
            row[i] = -1.0
            for j in np.argsort(row)[::-1][: COPYMOVE_TOPK]:
                if row[j] < _SIM_THRESHOLD:
                    break
                x1, y1, _ = blocks[i]
                x2, y2, _ = blocks[int(j)]
                dx, dy = int(x2 - x1), int(y2 - y1)
                if dx * dx + dy * dy < min_dist * min_dist:
                    continue
                phase_candidates.append((int(x1), int(y1), int(x2), int(y2)))
                if len(candidates) + len(phase_candidates) >= COPYMOVE_MAX_PAIRS:
                    break
            if len(candidates) + len(phase_candidates) >= COPYMOVE_MAX_PAIRS:
                break
        candidates.extend(phase_candidates)
        if len(candidates) >= COPYMOVE_MAX_PAIRS:
            break
    return candidates, n_blocks


def _cluster_shifts(shifts: list[tuple[int, int]]) -> Counter:
    """Quantize shift vectors into buckets and count cluster sizes."""
    t = _SHIFT_TOLERANCE
    return Counter((int(round(dx / t)), int(round(dy / t))) for dx, dy in shifts)


def _fft_shift_hypotheses(gray_f: Any) -> list[tuple[int, int]]:
    """Shift hypotheses from peaks of the mean-removed autocorrelation
    surface (FFT). A duplicated region correlates with itself at exactly
    its paste offset, so the true shift is a LOCAL MAXIMUM of the surface
    — even when the duplicated content is keypoint-free texture.

    Cheap (~10ms at 1MP): enumerates ALL offsets at once, in contrast to
    block lattices which only see multiples of their stride.
    """
    h, w = gray_f.shape[:2]
    # Mean-remove first, or the DC term dominates every peak.
    centered = gray_f - float(gray_f.mean())
    n = 1
    while n < 2 * max(h, w):
        n *= 2
    F = np.fft.rfft2(centered, s=(n, n))
    corr = np.fft.irfft2(F * np.conj(F), s=(n, n))
    corr = corr[:h, :w]
    if corr.size == 0 or float(corr.max()) <= 0:
        return []
    corr /= float(corr[0, 0]) or 1.0

    peaks: list[tuple[float, int, int]] = []
    m = _FFT_NMS_RADIUS
    for dy in range(0, h, 2):
        for dx in range(0, w, 2):
            v = corr[dy, dx]
            if v < _FFT_MIN_CORR or (dx * dx + dy * dy) < _FFT_MIN_LAG * _FFT_MIN_LAG:
                continue
            y0, y1 = max(0, dy - m), min(h, dy + m + 1)
            x0, x1 = max(0, dx - m), min(w, dx + m + 1)
            if v < float(corr[y0:y1, x0:x1].max()) - 1e-12:
                continue  # not a local max (3x3 NMS window)
            peaks.append((float(v), dx, dy))
    peaks.sort(reverse=True)
    out: list[tuple[int, int]] = []
    taken: list[tuple[int, int]] = []
    for v, dx, dy in peaks:
        if any((dx - tx) ** 2 + (dy - ty) ** 2 < _FFT_NMS_RADIUS * _FFT_NMS_RADIUS
               for tx, ty in taken):
            continue
        taken.append((dx, dy))
        out.append((dx, dy))
        if len(out) >= _FFT_MAX_PEAKS:
            break
    return out


def analyze_copymove(gray_u8: Any) -> dict[str, Any]:
    """Detect copy-move duplication via keypoint/block matching + shift clustering."""
    h, w = gray_u8.shape[:2]
    if min(h, w) < 120:
        return {
            "fired": False, "confidence": 0.0,
            "note": "Image too small for copy-move analysis.",
            "pairs": [], "clusters": [],
        }

    # Downscale for tractability — copy-move survives moderate rescaling.
    scale = 1.0
    if max(h, w) > COPYMOVE_MAX_DIM:
        scale = COPYMOVE_MAX_DIM / max(h, w)
        work = cv2.resize(gray_u8, (int(w * scale), int(h * scale)),
                          interpolation=cv2.INTER_AREA)
    else:
        work = gray_u8
    wh, ww = work.shape[:2]
    inv = 1.0 / scale
    gray_f = work.astype(np.float32) / 255.0

    block = COPYMOVE_BLOCK
    min_dist = COPYMOVE_MIN_PAIR_DISTANCE

    # --- candidate generation (two complementary sources) ------------------
    orb_pairs = _orb_candidates(work, min_dist)
    dct_pairs, n_blocks = _dct_candidates(gray_f, wh, ww)

    # --- NCC verification + refinement -------------------------------------
    pairs: list[dict[str, Any]] = []
    shifts: list[tuple[int, int]] = []
    for (x1, y1, x2, y2) in orb_pairs + dct_pairs:
        refined = _ncc_refine(gray_f, x1, y1, x2, y2, block)
        if refined is None or refined[0] < _NCC_THRESHOLD:
            continue
        _ncc, dx, dy = refined
        pairs.append({"x1": x1, "y1": y1, "x2": x1 + dx, "y2": y1 + dy,
                      "ncc": round(refined[0], 3)})
        shifts.append((dx, dy))
        if len(pairs) >= COPYMOVE_MAX_PAIRS:
            break

    # NOTE: no early return when ``pairs`` is empty — the FFT hypothesis
    # source below does not depend on block/keypoint matches at all.

    clusters = _cluster_shifts(shifts)

    # --- periodic-family suppression --------------------------------------
    # Repeated layouts produce MANY collinear clusters at multiples of one
    # base vector (the text/layout pitch), e.g. (30,60)+(25,50) → base (1,2).
    # Suppress a family when its ACCUMULATED pair count along one base
    # direction is significant — no single cluster needs to be large, which
    # is exactly how machine-printed pages evade a per-cluster test.
    families: dict[tuple[int, int], int] = {}
    for (qx, qy), cnt in clusters.items():
        if cnt < 2:
            continue
        g = gcd(abs(qx), abs(qy)) or 1
        base = (abs(qx) // g, abs(qy) // g)
        families[base] = families.get(base, 0) + cnt
    periodic_bases = {
        base for base, n in families.items() if n >= COPYMOVE_CLUSTER_MIN_PAIRS
    }

    def _is_periodic(qshift: tuple[int, int]) -> bool:
        g = gcd(abs(qshift[0]), abs(qshift[1])) or 1
        return (abs(qshift[0]) // g, abs(qshift[1]) // g) in periodic_bases

    suppressed_points = sum(cnt for q, cnt in clusters.items() if _is_periodic(q))
    remaining = [(q, cnt) for q, cnt in clusters.most_common() if not _is_periodic(q)]

    t = _SHIFT_TOLERANCE

    # --- shift-hypothesis sources -----------------------------------------
    # ORB/DCT pairs are clustered; the FFT surface contributes its own
    # hypotheses (arbitrary offsets, keypoint-free texture). EVERY source's
    # output is filtered through the SAME periodic-base test: a periodic
    # layout (repeated text lines) puts strong peaks at multiples of the
    # layout pitch on the FFT surface too, and those are exactly the shifts
    # that manufacture dense text-line 'agreement' on pristine pages.
    fft_hyps = [xy for xy in _fft_shift_hypotheses(gray_f) if not _is_periodic(xy)]
    if not remaining and not fft_hyps:
        return {
            "fired": False, "confidence": 0.0,
            "note": (
                "Dominant block repetition is periodic (repeating layout "
                f"pattern: {suppressed_points} pairs in collinear shift "
                "families) — not duplication evidence."
            ),
            "pairs": [], "clusters": [],
            "periodic_suppressed": suppressed_points,
        }

    # --- shift verification (the decisive evidence) -----------------------
    # Keypoint/lattice matches only HYPOTHESIZE shifts. For each candidate
    # shift, translate the image by that vector and measure the fraction of
    # textured pixels that agree closely — a real duplication shows a large,
    # compact region of near-perfect agreement. This is robust regardless
    # of how many feature matches survived the thresholds.
    # Textured-pixel mask at block granularity, upsampled to full image size
    # so it can be ANDed with the per-pixel diff inside _verify_shift.
    grid_h = wh // COPYMOVE_STRIDE + 1
    grid_w = ww // COPYMOVE_STRIDE + 1
    tile_stds = np.zeros((grid_h, grid_w), dtype=np.float32)
    for ty in range(grid_h):
        for tx in range(grid_w):
            y0, x0 = ty * COPYMOVE_STRIDE, tx * COPYMOVE_STRIDE
            patch = gray_f[y0: y0 + COPYMOVE_STRIDE, x0: x0 + COPYMOVE_STRIDE]
            tile_stds[ty, tx] = patch.std() if patch.size else 0.0
    texture_small = (tile_stds > COPYMOVE_TEXTURE_FLOOR / 255.0).astype(np.uint8)
    texture = cv2.resize(texture_small, (ww, wh), interpolation=cv2.INTER_NEAREST) > 0

    def _verify_shift(dx: int, dy: int) -> tuple[float, int, int, int, float, Any]:
        """Verify one exact (dx, dy) shift on the pixels.

        Returns ``(agree_frac, best_area, bw, bh, fill_ratio, good_mask)``
        where the bbox/density come from the DENSEST connected component of
        agreeing textured pixels — the real duplicated patch — instead of the
        global bounding box (which spans source + destination patches).
        """
        shifted = np.roll(np.roll(gray_f, dy, axis=0), dx, axis=1)
        diff = np.abs(gray_f - shifted)
        valid = np.ones_like(diff, dtype=bool)
        valid[: max(dy, 0), :] = False
        valid[:, : max(dx, 0)] = False
        if dy < 0:
            valid[wh + dy:, :] = False
        if dx < 0:
            valid[:, ww + dx:] = False
        # Agreement on textured pixels only (flat paper agrees trivially).
        good = (diff < _AGREE_DIFF) & valid & texture
        area = int(good.sum())
        if area == 0:
            return 0.0, 0, 0, 0, 0.0, good
        n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            good.astype(np.uint8), connectivity=8)
        if n <= 1:
            return 0.0, 0, 0, 0, 0.0, good
        # Densest component = the duplicated patch; stripes from repeated
        # layouts have low fill (thin bbox, sparse ink), so they lose here.
        k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        bw = int(stats[k, cv2.CC_STAT_WIDTH])
        bh = int(stats[k, cv2.CC_STAT_HEIGHT])
        comp_area = int(stats[k, cv2.CC_STAT_AREA])
        fill = comp_area / float(bw * bh) if bw * bh else 0.0
        frac = area / float(ww * wh)
        return float(frac), comp_area, bw, bh, float(fill), good

    # Exact shift per bucket: bucket centers quantize to 4px, and a real
    # duplication only agrees pixel-perfectly at its EXACT offset — so the
    # median of the raw member shifts is what gets verified, not the center.
    by_bucket: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for s in shifts:
        by_bucket.setdefault(
            (int(round(s[0] / t)), int(round(s[1] / t))), []
        ).append(s)

    # --- verify cluster hypotheses + FFT hypotheses -------------------------
    hyps: list[tuple[int, int, str]] = []
    for qshift, cnt in remaining[:_VERIFY_TOP_CLUSTERS]:
        if cnt < _VERIFY_MIN_PAIRS:
            continue
        members_s = by_bucket.get(qshift) or []
        if members_s:
            dx = int(np.median([s[0] for s in members_s]))
            dy = int(np.median([s[1] for s in members_s]))
        else:
            dx, dy = int(round(qshift[0] * t)), int(round(qshift[1] * t))
        if dx == 0 and dy == 0:
            continue
        hyps.append((dx, dy, f"cluster:{qshift}"))
    for dx, dy in fft_hyps:
        if (dx, dy) == (0, 0):
            continue
        hyps.append((dx, dy, "fft"))
    # Dedup identical offsets (cluster median may coincide with an FFT peak).
    seen_off: set[tuple[int, int]] = set()
    hyps = [h for h in hyps
            if (h[0], h[1]) not in seen_off and not seen_off.add((h[0], h[1]))]

    verified: list[tuple[tuple[int, int], tuple, str]] = []
    for dx, dy, src_tag in hyps:
        stats = _verify_shift(dx, dy)
        verified.append(((dx, dy), stats, src_tag))
    if not verified:
        return {
            "fired": False, "confidence": 0.0,
            "note": "Candidate shifts existed but none could be verified.",
            "pairs": [], "clusters": [],
            "periodic_suppressed": suppressed_points,
            "fft_hypotheses": len(fft_hyps),
        }

    # Pick the shift with the largest DENSE verified agreement (area × fill).
    verified.sort(key=lambda v: v[1][1] * v[1][4], reverse=True)
    (bdx, bdy), (agree_frac, area_px, src_w, src_h, fill_ratio, good_mask), \
        _src_tag = verified[0]
    q_best = (int(round(bdx / t)), int(round(bdy / t)))
    best_count = clusters.get(q_best, 0)
    support_kind = _src_tag.split(":")[0]

    # Distinct source positions in the winning cluster (for reporting).
    q_best = (int(round(bdx / t)), int(round(bdy / t)))
    members = [
        (p, s) for p, s in zip(pairs, shifts)
        if (int(round(s[0] / t)), int(round(s[1] / t))) == q_best
    ]
    cluster_pairs = [p for p, _s in members]

    # Density: the agreeing component must be a dense blob (a real
    # duplicated patch fills its bbox and is chunky in BOTH dimensions).
    # Repeated layouts agree in thin stripes (~16px tall text lines) that
    # never satisfy the min-side requirement.
    src_area_frac = (src_w * src_h) / float(ww * wh)
    compact = (
        fill_ratio >= _COMPACT_FILL_MIN
        and src_area_frac <= _COMPACT_BBOX_MAX
        and min(src_w, src_h) >= _MIN_SIDE_PX
    )

    fired = (
        area_px >= _MIN_AGREE_AREA          # at least ~30x30 px of agreement
        and agree_frac >= _MIN_AGREE_FRAC   # ≥0.8% of the page agrees
        and compact
    )

    # Independent 8x8 pixel-tile support under the winning exact shift: the
    # number of densely-agreeing tiles. This is an HONEST support count —
    # unlike lattice pair counts it does not depend on paste alignment.
    ts = _TILE_SUPPORT_SIZE
    th, tw = wh // ts, ww // ts
    tiles_full = good_mask[: th * ts, : tw * ts]
    tiles_full = tiles_full.reshape(th, ts, tw, ts).sum(axis=(1, 3))
    pair_count = int(((tiles_full >= ts * ts * _TILE_SUPPORT_MIN_FILL)).sum())

    confidence = 0.0
    severity = "info"
    if fired:
        confidence = min(0.85, 0.45 + agree_frac * 8.0)
        severity = "high" if agree_frac >= 0.05 else ("medium" if agree_frac >= 0.02 else "low")

    # Extent of the suspected duplication (10th-90th percentile of the
    # agreeing pixels under the winning exact shift).
    region = None
    if fired:
        ys_r, xs_r = np.nonzero(good_mask)
        x_lo, x_hi = int(np.percentile(xs_r, 10)), int(np.percentile(xs_r, 90))
        y_lo, y_hi = int(np.percentile(ys_r, 10)), int(np.percentile(ys_r, 90))
        region = {
            "x": int(x_lo * inv), "y": int(y_lo * inv),
            "w": int(max(1, x_hi - x_lo) * inv), "h": int(max(1, y_hi - y_lo) * inv),
        }

    return {
        "fired": fired,
        "confidence": round(confidence, 3),
        "severity": severity,
        "note": (
            f"Duplication verified: a {src_w}x{src_h}px region re-appears at "
            f"offset ({bdx},{bdy}) with {agree_frac:.1%} pixel agreement "
            f"— consistent with a duplicated region."
            if fired else
            f"No verified duplication "
            f"(best shift ~({bdx},{bdy}): agreement {agree_frac:.1%} "
            f"of page, {suppressed_points} periodic pairs suppressed)."
        ),
        "pairs": cluster_pairs[:30],
        "best_shift": [bdx, bdy],
        "pair_count": pair_count,
        "cluster_pair_count": int(clusters.get(q_best, 0)),
        "support_kind": support_kind,
        "verified_agreement_fraction": round(agree_frac, 4),
        "verified_region": {"w": int(src_w), "h": int(src_h)},
        "source_fill_ratio": round(fill_ratio, 3),
        "source_area_fraction": round(src_area_frac, 3),
        "region": region,
        "orb_candidates": len(orb_pairs),
        "dct_candidates": len(dct_pairs),
        "n_pairs_total": len(pairs),
        "periodic_suppressed": suppressed_points,
    }
