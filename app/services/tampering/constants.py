"""Shared verdict vocabulary + thresholds for the tampering module.

The four verdicts are deliberately honest about what heuristics can do:

- ``suspicious``       — at least one forensic signal fired, but none is
  individually conclusive (ELA glow alone is never proof of forgery).
- ``likely_manipulated`` — multiple independent signals agree; strong
  candidate for human escalation, still not a legal determination.
- ``inconclusive``     — image quality/coverage too poor to say anything
  either way (heavy compression, tiny image, analysis failure).
- ``no_obvious_manipulation`` — detectors ran and found nothing worth
  flagging; that is NOT a certificate of authenticity.
"""
from __future__ import annotations

from typing import Final

# --- Verdicts (the only four ever produced) ---------------------------------
VERDICT_SUSPICIOUS: Final[str] = "suspicious"
VERDICT_LIKELY: Final[str] = "likely_manipulated"
VERDICT_INCONCLUSIVE: Final[str] = "inconclusive"
VERDICT_CLEAN: Final[str] = "no_obvious_manipulation"

VERDICTS: Final[frozenset[str]] = frozenset(
    {VERDICT_SUSPICIOUS, VERDICT_LIKELY, VERDICT_INCONCLUSIVE, VERDICT_CLEAN}
)

# --- Severities --------------------------------------------------------------
SEV_INFO: Final[str] = "info"
SEV_LOW: Final[str] = "low"
SEV_MEDIUM: Final[str] = "medium"
SEV_HIGH: Final[str] = "high"

# --- Indicator types (one per forensic detector) -----------------------------
IND_METADATA = "metadata_anomaly"
IND_ELA = "ela_inconsistency"
IND_NOISE = "noise_inconsistency"
IND_COMPRESSION = "compression_inconsistency"
IND_COPYMOVE = "copy_paste_inconsistency"
IND_ARTIFACT = "image_editing_artifact"
IND_REGION = "region_anomaly"          # stamp/signature / photo-region cues
IND_MODEL = "model_verdict"            # trained classifier (when present)

# --- Signal → risk fusion ----------------------------------------------------
# Each indicator contributes ``weight * severity_factor`` risk points on a
# 0-100 scale; weights are heuristics, documented for explainability.
SEVERITY_FACTORS: Final[dict[str, float]] = {
    SEV_INFO: 0.0,
    SEV_LOW: 0.35,
    SEV_MEDIUM: 0.7,
    SEV_HIGH: 1.0,
}
INDICATOR_WEIGHTS: Final[dict[str, float]] = {
    IND_METADATA: 14.0,
    IND_ELA: 22.0,
    IND_NOISE: 20.0,
    IND_COMPRESSION: 16.0,
    IND_COPYMOVE: 26.0,     # duplicated patches are the most concrete cue
    IND_ARTIFACT: 18.0,
    IND_REGION: 15.0,
    IND_MODEL: 30.0,        # trained model, when present, is the strongest
}

# --- Aggregation / verdict thresholds ----------------------------------------
# Risk fusion: base (inconclusive) noise + sqrt-style saturating sum so that
# one weak signal never dominates, but several agreeing signals escalate.
BASE_NOISE_POINTS: Final[float] = 4.0        # adds humility to low scores
MAX_INDIVIDUAL_POINTS: Final[float] = 34.0   # cap per single indicator
RISK_CAP: Final[float] = 98.0                # never emit a perfect 100

# Verdict routing over the fused 0-100 risk score.
RISK_SUSPICIOUS_MIN: Final[float] = 20.0
RISK_LIKELY_MIN: Final[float] = 55.0

# Independent agreement needed for ``likely_manipulated`` even at lower risk.
LIKELY_AGREEING_INDICATORS: Final[int] = 3

# A detector firing at >= this confidence counts as "agreement".
AGREEMENT_CONFIDENCE_MIN: Final[float] = 0.55

# Coverage: fraction of the image whose pixels were actually analyzed at
# sufficient resolution. Below this the verdict can only be ``inconclusive``.
INCONCLUSIVE_COVERAGE_MAX: Final[float] = 0.45

# --- Image limits (decompression-bomb guard) ---------------------------------
MAX_ANALYSIS_PIXELS: Final[int] = 12_000_000   # ~12 MP before downscale
MIN_ANALYSIS_WIDTH: Final[int] = 250           # below ⇒ inconclusive
MIN_ANALYSIS_HEIGHT: Final[int] = 180
MAX_DIMENSION: Final[int] = 3000               # hard cap on any side

# ELA grid tiling (images larger than the tile are analyzed in tiles).
ELA_TILE_SIZE: Final[int] = 512
ELA_TILE_OVERLAP: Final[int] = 64

# JPEG grid check: 8x8 block grid coherence per tile.
JPEG_GRID_TILE: Final[int] = 256

# Noise-inconsistency analysis tile size (local high-frequency energy).
NOISE_TILE: Final[int] = 96

# Copy-move: block-match parameters (kept modest for CPU-time honesty).
COPYMOVE_MAX_DIM: Final[int] = 700             # working resolution for matching
COPYMOVE_BLOCK: Final[int] = 16
COPYMOVE_STRIDE: Final[int] = 8
COPYMOVE_TOPK: Final[int] = 4
COPYMOVE_MIN_PAIR_DISTANCE: Final[int] = 32    # pairs closer than this are ignored
COPYMOVE_TEXTURE_FLOOR: Final[float] = 6.0     # blocks flatter than this carry no evidence
COPYMOVE_CLUSTER_MIN_PAIRS: Final[int] = 6
COPYMOVE_MAX_PAIRS: Final[int] = 4000

# Stamp/signature region cues.
STAMP_COLOR_SAT_MIN: Final[float] = 0.25       # HSV saturation floor for ink
STAMP_HUE_SPREAD_MAX: Final[float] = 40.0      # degrees — single-ink consistency

# Photo-region (photo replacement) cues at the portrait boundary.
PHOTO_REGION_MIN_SIDE: Final[int] = 48
PHOTO_EDGE_SHARP_RATIO: Final[float] = 1.9     # boundary vs interior edge energy
PHOTO_ELA_EDGE_RATIO: Final[float] = 1.8       # boundary vs interior ELA energy

# Metadata gates (thresholds for calling metadata anomalies).
METADATA_SOFTWARE_KEYS: Final[tuple[str, ...]] = (
    "Software", "ProcessingSoftware", "Artist", "XPAuthor", "XPComment",
    "ImageHistory", "HistorySoftwareAgent",
)
METADATA_ANON_SOFTWARE_TOKENS: Final[tuple[str, ...]] = (
    "photoshop", "gimp", "lightroom", "paint.net", "pixelmator", "affinity",
    "snapseed", "picsart", "canva", "illustrator", "corel", "acdsee",
    "capture one", "darktable", "krita", "figma", "pixel", "pica",
)
CAMERA_SOFTWARE_TOKENS: Final[tuple[str, ...]] = (
    "microsoft", "windows", "apple", "ios", "android", "google", "samsung",
    "huawei", "xiaomi", "oneplus", "motorola", "iphone", "pixel camera",
)
# JPEG quantization tables that are NOT from a camera — most editors write
# their own table set; cameras overwhelmingly use standard table 0.
EDITED_QT_STD_TABLES: Final[frozenset[int]] = frozenset({0, 1})
