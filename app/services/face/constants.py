"""Shared vocabulary + thresholds for the face-verification module.

One-to-one verification only: the module decides whether the portrait on
the document and the presented (live) image are consistent with the SAME
person. It never searches a population database, and every failure mode
(no face, several faces, bad lighting, blur, extreme pose, occlusion)
honestly degrades the result instead of guessing a match.

Cosine similarity comes from SFace embeddings (512-d, ArcFace-style
training): +1 identical, ~0 unrelated. Empirically, same-person pairs land
well above 0.36 and different-person pairs below ~0.2 — thresholds are
configurable via environment so a deployment can calibrate them.
"""
from __future__ import annotations

from typing import Final

# --- Match vocabulary (the only three statuses ever produced) ---------------
MATCH: Final[str] = "MATCH"
NO_MATCH: Final[str] = "NO_MATCH"
VERDICT_INCONCLUSIVE: Final[str] = "INCONCLUSIVE"

VERDICTS: Final[frozenset[str]] = frozenset({MATCH, NO_MATCH, VERDICT_INCONCLUSIVE})

# --- Similarity decision thresholds (cosine, SFace 512-d) --------------------
# >= match threshold  → MATCH
# <= no-match ceiling → NO_MATCH
# in between          → INCONCLUSIVE (honest gray zone, routed to humans)
SIMILARITY_MATCH_THRESHOLD: Final[float] = 0.363   # OpenCV SFace reference value
SIMILARITY_NO_MATCH_MAX: Final[float] = 0.25

# --- Detection / quality gates ----------------------------------------------
# YuNet input size (the detector rescales internally, so this is a cap, not
# a requirement) — see detection.py.
DETECT_INPUT_MAX_SIDE: Final[int] = 640

# A face box smaller than this side length carries too few pixels for a
# reliable 112x112-aligned embedding (document portraits often sit at 80-150px).
MIN_FACE_SIDE_PX: Final[int] = 48

# Minimum acceptable resolution of the whole probe image.
MIN_IMAGE_SIDE_PX: Final[int] = 120

# --- Image-quality safeguards (probe + document portrait) --------------------
# Blur: variance of the Laplacian on the face crop (0-255 scale). Below this
# the face is too soft to embed reliably.
BLUR_LAPLACIAN_MIN: Final[float] = 35.0

# Lighting: mean luminance window of a usable face crop (0-255) and the
# maximum fraction of blown-out (>= 245) pixels.
BRIGHTNESS_MIN: Final[float] = 45.0
BRIGHTNESS_MAX: Final[float] = 225.0
OVEREXPOSURE_MAX_FRACTION: Final[float] = 0.60

# Lighting uniformity: std of luminance inside the face box — very low means
# the crop is featureless (flat gray patch), very high means harsh shadows.
FLATNESS_STD_MIN: Final[float] = 8.0

# Occlusion: fraction of the face box occupied by very dark pixels (hair,
# hand, mask shadow). Above this the face is considered covered.
OCCLUSION_DARK_MAX_FRACTION: Final[float] = 0.55

# Pose: YuNet reports five landmarks (eyes, nose tip, mouth corners). The
# ratio eye-center→nose horizontal offset to inter-eye distance estimates yaw;
# vertical offset estimates pitch. Beyond these ratios the face is turned too
# far for a frontal embedding to be meaningful.
POSE_YAW_MAX_RATIO: Final[float] = 0.45
POSE_PITCH_MAX_RATIO: Final[float] = 0.60

# Eye-openness heuristic: the standard deviation of luminance across each eye
# region (a closed eye is a smooth skin band; an open eye has iris/lash edges).
# Below this on BOTH eyes the eyes are treated as closed → occlusion cue.
EYE_REGION_STD_MIN: Final[float] = 10.0

# Multi-face: more than one confident face in the PROBE frame is ambiguous
# (who is the presenter?); the verdict must be inconclusive.
MAX_PROBE_FACES: Final[int] = 1

# Detection confidence floor for YuNet boxes (0-1). Real faces typically
# score 0.8+; 0.5 keeps recall on soft/low-quality document scans while
# still filtering spurious boxes.
DETECT_SCORE_MIN: Final[float] = 0.5

# --- Embedding / comparison ---------------------------------------------------
# The OpenCV Zoo SFace ONNX model emits 128-d embeddings (a 512-d ArcFace
# variant can be registered as a custom backend; cosine comparison is
# dimension-agnostic). Custom backends should document their own dimension.
EMBEDDING_DIM: Final[int] = 128
# Cosine similarity is computed on L2-normalized embeddings; this is the
# epsilon guarding the division.
_SIM_EPS: Final[float] = 1e-8

# --- Verdict confidence shaping ------------------------------------------------
# Confidence in the MATCH/NO_MATCH verdict itself (0-1). Grows with margin
# above/below the threshold and decays with image-quality deductions.
MATCH_CONF_BASE: Final[float] = 0.82
MATCH_CONF_MARGIN_SPAN: Final[float] = 0.25   # margin at which confidence saturates
QUALITY_CONF_PENALTY_MAX: Final[float] = 0.35 # worst quality ⇒ -0.35 confidence

# Inconclusive verdicts carry a fixed, honest confidence.
INCONCLUSIVE_CONFIDENCE: Final[float] = 0.3

# Stage-level confidence reported to the pipeline for MATCH/NO_MATCH verdicts.
STAGE_CONF_MAX: Final[float] = 0.95

# --- Model files -----------------------------------------------------------------
YUNET_FILENAME: Final[str] = "face_detection_yunet_2023mar.onnx"
SFACE_FILENAME: Final[str] = "face_recognition_sface_2021dec.onnx"
