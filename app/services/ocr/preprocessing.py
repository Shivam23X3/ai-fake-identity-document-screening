"""Image preprocessing for the OCR module.

Pipeline (each step optional/tunable):

    load → EXIF-rotation fix → manual rotation → perspective correction
         → resize to target width → noise reduction → contrast enhancement
         → grayscale/adaptive threshold → save

Returns a JSON-serializable report (paths + quality metrics) so it can be
embedded in pipeline stage output. Raises ``PreprocessError`` on failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class PreprocessError(RuntimeError):
    """Raised when an image cannot be read or processed."""


@dataclass
class PreprocessResult:
    """Everything downstream needs to know about the cleaned image."""

    path: str                 # processed image on disk
    original_path: str
    width: int
    height: int
    original_width: int
    original_height: int
    rotation_applied: float   # degrees, clockwise, applied manually (not EXIF)
    deskew_angle: float       # degrees, auto-corrected skew
    perspective_corrected: bool
    blur_removed: bool
    contrast_gain: float
    quality: dict[str, float]   # sharpness, brightness, contrast, etc.
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "original_path": self.original_path,
            "width": self.width,
            "height": self.height,
            "original_width": self.original_width,
            "original_height": self.original_height,
            "rotation_applied": self.rotation_applied,
            "deskew_angle": self.deskew_angle,
            "perspective_corrected": self.perspective_corrected,
            "blur_removed": self.blur_removed,
            "contrast_gain": round(self.contrast_gain, 3),
            "quality": {k: round(v, 2) for k, v in self.quality.items()},
            "warnings": self.warnings,
        }


def _load_image(path: str) -> np.ndarray:
    # imread cannot handle non-ASCII paths on Windows; read bytes ourselves.
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        raise PreprocessError(f"Could not read image file: {path}")
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise PreprocessError(f"Could not decode image: {path}")
    return img


def _fix_exif_rotation(path: str) -> np.ndarray:
    """Use Pillow's transposition (respects EXIF Orientation) when available."""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            return cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:  # noqa: BLE001 - fall back to the cv2 decode
        return _load_image(path)


def _apply_rotation(img: np.ndarray, degrees: float) -> np.ndarray:
    if degrees == 0:
        return img
    if degrees == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    h, w = img.shape[:2]
    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, degrees, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w = int(h * sin + w * cos)
    new_h = int(h * cos + w * sin)
    matrix[0, 2] += new_w / 2 - center[0]
    matrix[1, 2] += new_h / 2 - center[1]
    return cv2.warpAffine(img, matrix, (new_w, new_h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _estimate_skew_angle(gray: np.ndarray) -> float:
    """Estimate text-line skew in [-15, +15] degrees via the Hough transform."""
    small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    edges = cv2.Canny(small, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                            minLineLength=small.shape[1] // 4, maxLineGap=12)
    if lines is None:
        return 0.0
    angles: list[float] = []
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        # Near-horizontal lines are text lines / document edges.
        if abs(angle) <= 15:
            angles.append(angle)
    if not angles:
        return 0.0
    return float(np.median(angles))


def _deskew(img: np.ndarray, gray: np.ndarray) -> tuple[np.ndarray, float]:
    angle = _estimate_skew_angle(gray)
    if abs(angle) < 0.4:  # noise floor
        return img, 0.0
    corrected = _apply_rotation(img, angle)
    return corrected, angle


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    rect[0] = pts[np.argmin(s)]          # TL has smallest x+y
    rect[2] = pts[np.argmax(s)]          # BR largest x+y
    rect[1] = pts[np.argmin(diff)]       # TR smallest y-x
    rect[3] = pts[np.argmax(diff)]       # BL largest y-x
    return rect


def _try_perspective_correction(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """Straighten the document when a clear quad contour dominates the frame."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    # Otsu threshold isolates the bright document on a darker background.
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thresh = cv2.morphologyEx(
        thresh, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)),
    )
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return img, False

    page_area = h * w
    best: np.ndarray | None = None
    best_area = 0.0
    for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area = cv2.contourArea(cnt)
        if area < 0.3 * page_area:  # must dominate the frame
            break
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = _order_corners(approx.reshape(4, 2).astype(np.float32))
            if area > best_area:
                best, best_area = quad, area
    if best is None:
        return img, False

    # Reject quads whose opposite sides are wildly unequal (bad detection).
    (tl, tr, br, bl) = best
    top, bottom = np.linalg.norm(tr - tl), np.linalg.norm(br - bl)
    left, right = np.linalg.norm(bl - tl), np.linalg.norm(br - tr)
    ratios = [a / b for a, b in ((top, bottom), (bottom, top), (left, right), (right, left))]
    if max(ratios) > 1.35:
        return img, False

    dest_w = int(max(top, bottom))
    dest_h = int(max(left, right))
    if dest_w < 100 or dest_h < 100:
        return img, False
    matrix = cv2.getPerspectiveTransform(best, np.array(
        [[0, 0], [dest_w - 1, 0], [dest_w - 1, dest_h - 1], [0, dest_h - 1]],
        dtype=np.float32,
    ))
    return cv2.warpPerspective(img, matrix, (dest_w, dest_h)), True


def _resize(img: np.ndarray, target_width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if target_width <= 0 or w == target_width:
        return img
    scale = target_width / w
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(img, (target_width, max(1, int(h * scale))), interpolation=interp)


def _reduce_noise(img: np.ndarray) -> tuple[np.ndarray, bool]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if cv2.Laplacian(gray, cv2.CV_64F).var() < 40:  # noisy/soft image
        return cv2.fastNlMeansDenoisingColored(img, None, 5, 5, 7, 21), True
    return img, False


def _enhance_contrast(img: np.ndarray) -> tuple[np.ndarray, float]:
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    before = float(l_ch.std())
    l_ch = clahe.apply(l_ch)
    after = float(l_ch.std())
    merged = cv2.merge((l_ch, a_ch, b_ch))
    return cv2.cvtColor(merged, cv2.COLOR_LAB2BGR), (after - before) / max(before, 1e-6)


def _quality_metrics(gray: np.ndarray) -> dict[str, float]:
    return {
        "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "brightness": float(gray.mean()),
        "contrast": float(gray.std()),
    }


def preprocess(
    image_path: str,
    *,
    output_dir: str | Path | None = None,
    rotation: float = 0.0,
    target_width: int = 1400,
    do_perspective: bool = True,
    do_denoise: bool = True,
    do_contrast: bool = True,
    do_deskew: bool = True,
    save_grayscale: bool = False,
) -> PreprocessResult:
    """Run the full preprocessing chain and persist the cleaned image.

    Parameters
    ----------
    rotation:
        Manual rotation hint in degrees (0/90/180/270 or arbitrary).
    """
    src = Path(image_path)
    if not src.is_file():
        raise PreprocessError(f"Image not found: {image_path}")

    out_dir = Path(output_dir) if output_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "preprocessed.png"

    img = _fix_exif_rotation(str(src))
    orig_w, orig_h = img.shape[1], img.shape[0]
    warnings: list[str] = []

    img = _apply_rotation(img, float(rotation) % 360)

    # Small resize BEFORE deskew/perspective: heavy enough to be fast,
    # light enough to keep text legible.
    img = _resize(img, min(2200, max(target_width, orig_w)))

    perspective_corrected = False
    if do_perspective:
        img, perspective_corrected = _try_perspective_correction(img)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    deskew_angle = 0.0
    if do_deskew:
        img, deskew_angle = _deskew(img, gray)
        if deskew_angle:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    img = _resize(img, target_width)

    blur_removed = False
    if do_denoise:
        img, blur_removed = _reduce_noise(img)

    contrast_gain = 0.0
    if do_contrast:
        img, contrast_gain = _enhance_contrast(img)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    quality = _quality_metrics(gray)

    if quality["sharpness"] < 25:
        warnings.append("Image appears blurry; OCR confidence may be reduced.")
    if quality["brightness"] < 60 or quality["brightness"] > 200:
        warnings.append("Unusual brightness; consider retaking the photo.")

    if save_grayscale:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        out_path = out_dir / "preprocessed_gray.png"

    ok = cv2.imwrite(str(out_path), img)
    if not ok:
        raise PreprocessError(f"Failed to write preprocessed image: {out_path}")

    return PreprocessResult(
        path=str(out_path),
        original_path=str(src),
        width=img.shape[1],
        height=img.shape[0],
        original_width=orig_w,
        original_height=orig_h,
        rotation_applied=float(rotation) % 360,
        deskew_angle=round(deskew_angle, 2),
        perspective_corrected=perspective_corrected,
        blur_removed=blur_removed,
        contrast_gain=contrast_gain,
        quality=quality,
        warnings=warnings,
    )
