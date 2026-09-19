"""Face detection (YuNet via cv2.FaceDetectorYN) + landmark-based pose/occlusion cues.

YuNet returns up to N boxes with 5 landmarks (right eye, left eye, nose tip,
right mouth corner, left mouth corner) and a confidence score. From those
landmarks the module derives honest safeguards:

- yaw  ≈ horizontal eye-midpoint→nose offset / inter-eye distance
- pitch ≈ vertical eye-midpoint→nose offset / inter-eye distance
- eye-openness ≈ luminance std inside each eye neighborhood
- occlusion   ≈ fraction of very dark pixels inside the face box

The detector degrades to "no face" (never a guess) when weights are missing
or the image is unusable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.services.face.constants import (
    DETECT_INPUT_MAX_SIDE,
    DETECT_SCORE_MIN,
    EYE_REGION_STD_MIN,
    OCCLUSION_DARK_MAX_FRACTION,
    POSE_PITCH_MAX_RATIO,
    POSE_YAW_MAX_RATIO,
    YUNET_FILENAME,
)

logger = logging.getLogger(__name__)

_MODELS_DIR = Path(__file__).resolve().parents[3] / "models"

# NOTE on lifecycle: cv2.FaceDetectorYN instances do NOT cleanly re-init when
# setInputSize() is called twice with different sizes in one process (the
# second detect() can throw "Size does not match" or silently return None).
# The YuNet weights are tiny (~230 KB), so we simply build a fresh detector
# per detect_faces() call — cheap and always correct.
_NEW_DETECTOR_PER_CALL = True


def yunet_model_path() -> Path:
    return _MODELS_DIR / YUNET_FILENAME


def _get_detector() -> Any:
    """Build a fresh YuNet detector (None when weights are absent).

    A new instance per call avoids cv2.FaceDetectorYN's unreliable state
    reuse across setInputSize calls with different sizes.
    """
    path = yunet_model_path()
    if not path.is_file():
        return None
    try:
        detector = cv2.FaceDetectorYN.create(str(path), "", (320, 320))
        detector.setScoreThreshold(DETECT_SCORE_MIN)
        return detector
    except Exception as exc:  # noqa: BLE001 - broken weights must not crash
        logger.warning("YuNet failed to load: %s", exc)
        return None


def yunet_available() -> bool:
    return _get_detector() is not None


@dataclass
class FaceBox:
    """One detected face with landmarks and derived safeguard cues."""

    x: int
    y: int
    w: int
    h: int
    score: float
    right_eye: tuple[int, int] | None = None
    left_eye: tuple[int, int] | None = None
    nose: tuple[int, int] | None = None
    mouth_right: tuple[int, int] | None = None
    mouth_left: tuple[int, int] | None = None
    yaw_ratio: float | None = None
    pitch_ratio: float | None = None
    eyes_closed: bool = False
    dark_fraction: float = 0.0
    quality_issues: list[Any] = field(default_factory=list)

    @property
    def box(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def to_dict(self) -> dict[str, Any]:
        return {
            "box": self.box,
            "score": round(float(self.score), 3),
            "yaw_ratio": round(self.yaw_ratio, 3) if self.yaw_ratio is not None else None,
            "pitch_ratio": round(self.pitch_ratio, 3) if self.pitch_ratio is not None else None,
            "eyes_closed": self.eyes_closed,
            "dark_fraction": round(float(self.dark_fraction), 3),
        }


def _decode(bgr_or_path: Any) -> Any:
    if isinstance(bgr_or_path, (str, Path)):
        data = np.fromfile(str(bgr_or_path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    return bgr_or_path


def _downscale(img: Any) -> tuple[Any, float]:
    h, w = img.shape[:2]
    scale = 1.0
    if max(h, w) > DETECT_INPUT_MAX_SIDE:
        scale = DETECT_INPUT_MAX_SIDE / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img, scale


def _to_int(pt: Any) -> tuple[int, int]:
    return int(round(float(pt[0]))), int(round(float(pt[1])))


def _pose_ratios(right_eye, left_eye, nose) -> tuple[float | None, float | None]:
    if not (right_eye and left_eye and nose):
        return None, None
    ex = (right_eye[0] + left_eye[0]) / 2.0
    ey = (right_eye[1] + left_eye[1]) / 2.0
    inter_eye = float(np.hypot(left_eye[0] - right_eye[0], left_eye[1] - right_eye[1]))
    if inter_eye < 1.0:
        return None, None
    yaw = abs(nose[0] - ex) / inter_eye
    pitch = abs(nose[1] - ey) / inter_eye
    return float(yaw), float(pitch)


def _eyes_closed(bgr: Any, box: "FaceBox") -> bool:
    """Both eye neighborhoods smooth (low luminance std) ⇒ eyes closed."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape[:2]
    if not (box.right_eye and box.left_eye):
        return False
    r = max(4, int(box.w * 0.12))
    stds = []
    for (cx, cy) in (box.right_eye, box.left_eye):
        x0, x1 = max(0, cx - r), min(W, cx + r)
        y0, y1 = max(0, cy - r // 2), min(H, cy + r // 2)
        if x1 <= x0 or y1 <= y0:
            return False
        stds.append(float(gray[y0:y1, x0:x1].std()))
    return bool(stds) and all(s < EYE_REGION_STD_MIN for s in stds)


def _dark_fraction(bgr: Any, box: "FaceBox") -> float:
    x0, y0 = max(0, box.x), max(0, box.y)
    x1 = min(bgr.shape[1], box.x + box.w)
    y1 = min(bgr.shape[0], box.y + box.h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    gray = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return float((gray < 40).mean())


def detect_faces(
    bgr_or_path: Any,
    score_threshold: float | None = None,
    multi_scale: bool = True,
) -> list[FaceBox]:
    """Detect faces in a BGR image (or image file path). Empty list = none found.

    ``score_threshold`` optionally overrides the detector's confidence floor
    (used by tests on synthetic imagery; production default stays strict).
    ``multi_scale`` retries once on a downscaled copy when the full-resolution
    pass finds nothing — document portraits are sometimes tightly framed,
    and YuNet is sensitive to face size relative to the frame.
    """
    img = _decode(bgr_or_path)
    if img is None or img.size == 0:
        return []
    detector = _get_detector()
    if detector is None:
        return []
    work, scale = _downscale(img)
    h, w = work.shape[:2]
    if h < 40 or w < 40:
        return []
    def _run(det: Any, img_work: Any, thr: float | None):
        hh, ww = img_work.shape[:2]
        det.setInputSize((ww, hh))
        if thr is not None:
            det.setScoreThreshold(max(0.1, min(0.9, float(thr))))
        return det.detect(img_work)

    try:
        _count, faces = _run(detector, work, score_threshold)
    except Exception as exc:  # noqa: BLE001 - detector must never kill the stage
        logger.warning("YuNet detect() failed: %s", exc)
        return []

    if faces is None and multi_scale and scale == 1.0:
        # Retry once at half resolution on a FRESH detector instance: a
        # tightly-framed face often detects better when framing is looser.
        try:
            small = cv2.resize(
                work, (max(40, w // 2), max(40, h // 2)), interpolation=cv2.INTER_AREA
            )
            retry_detector = _get_detector()
            if retry_detector is not None:
                _count, faces = _run(retry_detector, small, score_threshold)
                if faces is not None:
                    scale = 0.5
        except Exception as exc:  # noqa: BLE001
            logger.warning("YuNet multi-scale retry failed: %s", exc)

    out: list[FaceBox] = []
    inv = 1.0 / scale
    if faces is None:
        return out
    for f in np.asarray(faces):
        x, y, bw, bh = (float(v) for v in f[:4])
        score = float(f[14]) if len(f) > 14 else 0.0
        fx, fy = int(round(x * inv)), int(round(y * inv))
        fw, fh = max(1, int(round(bw * inv))), max(1, int(round(bh * inv)))
        landmarks = [_to_int(f[4 + 2 * i: 6 + 2 * i] * inv) for i in range(5)]
        fb = FaceBox(
            x=fx, y=fy, w=fw, h=fh, score=score,
            right_eye=landmarks[0], left_eye=landmarks[1], nose=landmarks[2],
            mouth_right=landmarks[3], mouth_left=landmarks[4],
        )
        fb.yaw_ratio, fb.pitch_ratio = _pose_ratios(fb.right_eye, fb.left_eye, fb.nose)
        fb.eyes_closed = _eyes_closed(img, fb)
        fb.dark_fraction = _dark_fraction(img, fb)
        out.append(fb)
    out.sort(key=lambda b: b.score, reverse=True)
    return out


def extreme_pose(box: FaceBox) -> bool:
    """True when any *known* pose ratio exceeds the frontal-pose limits."""
    yaw_bad = box.yaw_ratio is not None and box.yaw_ratio > POSE_YAW_MAX_RATIO
    pitch_bad = box.pitch_ratio is not None and box.pitch_ratio > POSE_PITCH_MAX_RATIO
    return bool(yaw_bad or pitch_bad)


def occluded(box: FaceBox) -> bool:
    """Occlusion heuristics: heavy dark coverage or both eyes closed."""
    return box.dark_fraction > OCCLUSION_DARK_MAX_FRACTION or box.eyes_closed
