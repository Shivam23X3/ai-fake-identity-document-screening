"""Tampering engine: the full forensic pipeline for one document.

    load + preprocess (guard size, extract photo/ink regions)
      → metadata analysis (detector 5)
      → ELA (detector 2)                    ← quality-aware reference JPEG
      → noise inconsistency (detector 3)
      → compression/grid analysis (detector 7)
      → copy-move (detector 6)
      → photo replacement (detector 1)
      → stamp/signature cues
      → generic editing artifacts (detector 4)
      → trained model (if registered)       ← optional, interface in model.py
      → fusion → risk 0-100 + verdict + indicators

Every detector is individually wrapped: one crashing detector never kills
the analysis, it only degrades the result (and feeds the failure list that
can route the verdict to ``inconclusive``).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.services.tampering.compression import analyze_compression
from app.services.tampering.constants import (
    MAX_ANALYSIS_PIXELS,
    MAX_DIMENSION,
    MIN_ANALYSIS_HEIGHT,
    MIN_ANALYSIS_WIDTH,
)
from app.services.tampering.copymove import analyze_copymove
from app.services.tampering.ela import analyze_ela, ela_region_flags, estimate_jpeg_quality
from app.services.tampering.fusion import fuse_signals
from app.services.tampering.metadata import analyze_metadata, read_metadata
from app.services.tampering.model import select_model
from app.services.tampering.noise import analyze_noise
from app.services.tampering.regions import (
    detect_editing_artifacts,
    detect_photo_region,
    detect_stamp_region,
)

logger = logging.getLogger(__name__)


class TamperingAnalysisError(RuntimeError):
    """Raised when the file cannot be analyzed at all."""


def _load_bgr(path: Path) -> Any:
    """Decode any supported image; handles non-ASCII paths on Windows."""
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        raise TamperingAnalysisError(f"Cannot read image file: {path}")
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise TamperingAnalysisError(f"Cannot decode image: {path}")
    return img


def _guard_size(img: Any) -> Any:
    h, w = img.shape[:2]
    if max(h, w) > MAX_DIMENSION or h * w > MAX_ANALYSIS_PIXELS:
        scale = min(MAX_DIMENSION / max(h, w),
                    (MAX_ANALYSIS_PIXELS / float(h * w)) ** 0.5)
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    return img


def _detect_jpeg(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(3)[:2] == b"\xff\xd8"
    except OSError:
        return False


def run_tampering_analysis(image_path: str | Path) -> dict[str, Any]:
    """Run the full forensic tampering pipeline on one image.

    Returns the Step-6 contract payload:
    ``{tampering_detected, risk_score(0-100), confidence(0-1),
    indicators[{type, severity, confidence}], verdict, ...}``.
    """
    started = time.perf_counter()
    path = Path(image_path)
    if not path.is_file():
        raise TamperingAnalysisError(f"Image not found: {path}")

    img = _guard_size(_load_bgr(path))
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray_f = gray.astype(np.float32) / 255.0

    warnings: list[str] = []
    detector_failures: list[str] = []
    indicators: list[dict[str, Any]] = []

    # --- coverage accounting (for the inconclusive verdict) ---------------
    small = w < MIN_ANALYSIS_WIDTH or h < MIN_ANALYSIS_HEIGHT
    if small:
        warnings.append(
            f"Image is small ({w}x{h}); forensic reliability is reduced."
        )

    # ------------------------------------------------------------------
    # Detector 5: metadata (works even when pixels are unreadable)
    # ------------------------------------------------------------------
    meta = read_metadata(path)
    indicators.extend(analyze_metadata(meta))

    is_jpeg = _detect_jpeg(path)

    # ------------------------------------------------------------------
    # Load ELA reference quality from the file (JPEG only)
    # ------------------------------------------------------------------
    ref_q: int | None = None
    if is_jpeg:
        ref_q = estimate_jpeg_quality(str(path))
        if ref_q is not None:
            ref_q = max(70, min(97, ref_q))

    # ------------------------------------------------------------------
    # ELA (detector 2)
    # ------------------------------------------------------------------
    ela = None
    try:
        ela = analyze_ela(img, reference_quality=ref_q)
        if ela.global_gate:
            warnings.append(
                "ELA uninformative: the whole image shows uniform error level "
                "(typical of heavily re-encoded or flat scans)."
            )
        else:
            flags = ela_region_flags(ela)
            strongest = next(iter(flags), None)
            if strongest and strongest["contrast"] >= 2.0 and strongest["area"] >= 900:
                severity = "high" if strongest["contrast"] >= 3.5 else "medium"
                conf = min(0.6, 0.3 + (strongest["contrast"] - 2.0) * 0.08)
                indicators.append(
                    {
                        "type": "ela_inconsistency",
                        "severity": severity,
                        "confidence": round(conf, 3),
                        "note": (
                            f"Localized error-level outlier ({strongest['w']}x{strongest['h']}px, "
                            f"{strongest['contrast']:.1f}x page mean). Consistent with local "
                            f"editing, but text/highlights cause the same effect — verify manually."
                        ),
                        "details": strongest,
                    }
                )
            elif strongest is None and not warnings:
                pass  # no ELA response at all: fine
    except Exception as exc:  # noqa: BLE001 - detector isolation
        logger.exception("ELA detector failed")
        detector_failures.append(f"ela: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Noise inconsistency (detector 3)
    # ------------------------------------------------------------------
    noise = None
    try:
        noise = analyze_noise(gray_f)
        if noise["fired"]:
            indicators.append(
                {
                    "type": "noise_inconsistency",
                    "severity": noise["severity"],
                    "confidence": noise["confidence"],
                    "note": noise["note"],
                    "details": {"outliers": noise["outliers"][:8]},
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Noise detector failed")
        detector_failures.append(f"noise: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Compression/grid (detector 7)
    # ------------------------------------------------------------------
    comp = None
    try:
        comp = analyze_compression(gray, is_jpeg=is_jpeg)
        if comp["fired"]:
            indicators.append(
                {
                    "type": "compression_inconsistency",
                    "severity": comp["severity"],
                    "confidence": comp["confidence"],
                    "note": comp["note"],
                    "details": {"outliers": comp["outliers"][:8]},
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Compression detector failed")
        detector_failures.append(f"compression: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Copy-move (detector 6)
    # ------------------------------------------------------------------
    copymove = None
    try:
        copymove = analyze_copymove(gray)
        if copymove["fired"]:
            cp_severity = copymove["severity"]
            # A "high" claim needs bulk: a duplicated patch must cover ≥2%
            # of the page. Thin stripes / fragmented agreement (typical of
            # repeated printed layouts) are capped at medium.
            agree_frac = float(copymove.get("verified_agreement_fraction") or 0.0)
            if cp_severity == "high" and agree_frac < 0.02:
                cp_severity = "medium"
            indicators.append(
                {
                    "type": "copy_paste_inconsistency",
                    "severity": cp_severity,
                    "confidence": copymove["confidence"],
                    "note": copymove["note"],
                    "details": {
                        "best_shift": copymove.get("best_shift"),
                        "pair_count": copymove.get("pair_count"),
                        "region": copymove.get("region"),
                    },
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Copy-move detector failed")
        detector_failures.append(f"copymove: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Photo replacement (detector 1)
    # ------------------------------------------------------------------
    photo = None
    try:
        photo = detect_photo_region(img, gray, ela) if ela else {"fired": False}
        if photo.get("fired"):
            indicators.append(
                {
                    "type": "region_anomaly",
                    "severity": photo["severity"],
                    "confidence": photo["confidence"],
                    "note": photo["note"],
                    "details": {"photo_box": photo.get("photo_box"), "cues": photo["cues"]},
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Photo-region detector failed")
        detector_failures.append(f"photo_region: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Stamp/signature cues
    # ------------------------------------------------------------------
    stamp = None
    try:
        stamp = detect_stamp_region(img)
        if stamp.get("fired"):
            indicators.append(
                {
                    "type": "region_anomaly",
                    "severity": stamp["severity"],
                    "confidence": stamp["confidence"],
                    "note": stamp["note"],
                    "details": {"cues": stamp["cues"]},
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stamp detector failed")
        detector_failures.append(f"stamp: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Generic editing artifacts (detector 4)
    # ------------------------------------------------------------------
    artifacts = None
    try:
        artifacts = detect_editing_artifacts(img, gray, ela) if ela else {"fired": False}
        if artifacts.get("fired"):
            indicators.append(
                {
                    "type": "image_editing_artifact",
                    "severity": artifacts["severity"],
                    "confidence": artifacts["confidence"],
                    "note": artifacts["note"],
                    "details": {"flags": artifacts["flags"][:8]},
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Artifact detector failed")
        detector_failures.append(f"artifacts: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Trained model (optional; interface in model.py)
    # ------------------------------------------------------------------
    model_probability: float | None = None
    model_info: dict[str, Any] = {"name": "none", "available": False}
    try:
        model = select_model()
        model_info = {"name": model.name, "available": bool(getattr(model, "available", lambda: True)())}
        if model_info["available"]:
            out = model.predict(img, gray)
            p = out.get("probability")
            if p is not None:
                model_probability = float(p)
                indicators.append(
                    {
                        "type": "model_verdict",
                        "severity": "high" if p >= 0.8 else ("medium" if p >= 0.5 else "low"),
                        "confidence": abs(p - 0.5) * 2.0,
                        "note": f"Trained model '{model.name}' probability {p:.2f}.",
                        "details": {k: v for k, v in out.items() if k != "map"},
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tampering model failed")
        detector_failures.append(f"model: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Coverage + fusion
    # ------------------------------------------------------------------
    usable_fraction = 1.0
    if noise and noise.get("usable_fraction") is not None:
        usable_fraction = float(noise["usable_fraction"])
    coverage = usable_fraction * (0.2 if small else 1.0)
    # Metadata-only analysis (no pixels) would drop coverage further, but
    # pixel decoding already succeeded here.

    fused = fuse_signals(
        indicators,
        coverage=coverage,
        detector_failures=detector_failures,
        model_probability=model_probability,
    )

    duration_ms = int((time.perf_counter() - started) * 1000)

    return {
        **fused,
        "engine": "forensic-heuristics-v1",
        "implemented": True,
        "image": {"width": int(w), "height": int(h), "format": meta.get("format")},
        "model": model_info,
        "warnings": warnings,
        "detectors": {
            "ela": {
                "mean_error": round(ela.mean_error, 2) if ela else None,
                "reference_quality": ela.reference_quality if ela else None,
                "global_gate": ela.global_gate if ela else None,
                "bright_fraction": round(ela.bright_fraction, 4) if ela else None,
            },
            "noise": {k: noise[k] for k in ("global_median_noise", "usable_fraction")} if noise else None,
            "compression": {"median_ratio": comp.get("median_ratio")} if comp else None,
            "copymove": {"pair_count": copymove.get("pair_count")} if copymove else None,
            "photo_region": photo.get("photo_box") if photo else None,
            "stamp": {"ink_fraction": stamp.get("ink_fraction")} if stamp else None,
        },
        "metadata_summary": {
            "has_exif": meta.get("has_exif"),
            "has_xmp": meta.get("has_xmp"),
            "software": (meta.get("tags") or {}).get("Software"),
        },
        "duration_ms": duration_ms,
    }
