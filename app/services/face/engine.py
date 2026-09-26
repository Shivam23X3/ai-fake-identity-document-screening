"""Face verification engine — one-to-one verification ONLY.

    document image ──▶ detect portrait face ──▶ quality checks ─▶ embedding
    presented image ─▶ detect presenter face ─▶ quality checks ─▶ embedding
                              ↓
        cosine similarity → threshold → MATCH / NO_MATCH / INCONCLUSIVE

Every safeguard (no face, multiple faces, blur, lighting, pose, occlusion,
missing model weights) routes the verdict to INCONCLUSIVE with an explicit
reason — the module never guesses. Similarity is ALWAYS reported so a human
reviewer sees the evidence even when the automatic verdict abstains.

The result matches the Step-7 contract exactly:

    {face_detected_document, face_detected_presented_person,
     similarity_score, match_status, confidence, ...}
"""
from __future__ import annotations

import logging
import time
from typing import Any

import cv2
import numpy as np

from app.services.face import detection
from app.services.face.constants import (
    INCONCLUSIVE_CONFIDENCE,
    MATCH,
    MAX_PROBE_FACES,
    MIN_FACE_SIDE_PX,
    MIN_IMAGE_SIDE_PX,
    NO_MATCH,
    QUALITY_CONF_PENALTY_MAX,
    SIMILARITY_MATCH_THRESHOLD,
    SIMILARITY_NO_MATCH_MAX,
    STAGE_CONF_MAX,
    VERDICT_INCONCLUSIVE,
)
from app.services.face.model import cosine_similarity, select_model
from app.services.face.quality import evaluate_quality

logger = logging.getLogger(__name__)


class FaceImageError(RuntimeError):
    """Raised when an input image cannot be decoded at all."""


def _presentation_cues(probe_bgr: Any, probe_face: detection.FaceBox | None) -> dict[str, Any]:
    """Passive, image-only presentation cues for the PROBE frame.

    Honest scope: these are heuristic observations that a reviewer can
    weigh — they are NOT a liveness guarantee and detect no dedicated
    attack class on their own. Every cue is reported with the evidence a
    human can re-check. Printed-photo re-presentation (a screen/printer
    moiré) and depth cues are explicitly OUT of scope; the verdict stays
    advisory and human review is mandatory regardless.
    """
    cues: dict[str, Any] = {
        "note": (
            "Passive image-only heuristics — NOT a liveness guarantee. "
            "A dedicated interactive liveness check (blink/turn challenge) "
            "is the production upgrade path."
        ),
    }
    if probe_bgr is None or probe_face is None:
        cues["assessed"] = False
        return cues
    h, w = probe_bgr.shape[:2]
    box = probe_face.box
    crop = probe_bgr[
        max(0, box["y"]): min(h, box["y"] + box["h"]),
        max(0, box["x"]): min(w, box["x"] + box["w"]),
    ]
    if crop.size == 0:
        cues["assessed"] = False
        return cues
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    # --- screen re-capture: periodic display banding -----------------------
    # A photograph of a screen often keeps a faint row/col-periodic pattern.
    # We measure the FFT energy at the first row/col harmonics of the crop.
    f = np.abs(np.fft.fftshift(np.fft.fft2(gray.astype(np.float32))))
    cy, cx = np.array(f.shape) // 2
    row_band = f[cy - 1: cy + 2, :].sum(axis=0)
    col_band = f[:, cx - 1: cx + 2].sum(axis=1)
    total = float(f.sum()) or 1.0
    band_energy = float(row_band[5:20].sum() + col_band[5:20].sum()) / total
    screen_band_suspect = band_energy > 0.02
    if screen_band_suspect:
        cues["screen_band_energy"] = round(band_energy, 4)

    # --- glare: blown-out specular highlights inside the face box ----------
    blown = float((gray >= 245).mean())
    glare_suspect = blown > 0.08
    if glare_suspect:
        cues["glow_blown_fraction"] = round(blown, 3)

    cues["assessed"] = True
    cues["suspicions"] = [
        {"code": "possible_screen_recapture", "note": "Periodic display banding detected in the face crop."}
    ] if screen_band_suspect else ([])
    if glare_suspect:
        cues["suspicions"].append(
            {"code": "heavy_glare", "note": "Large blown-out highlight area in the face region."}
        )
    if not cues.get("suspicions"):
        cues["suspicions"] = []
    return cues


def _load_bgr(path: str | Any) -> Any:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        raise FaceImageError(f"Cannot read image file: {path}")
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FaceImageError(f"Cannot decode image: {path}")
    return img


def _aligned_crop(bgr: Any, box: detection.FaceBox) -> Any:
    """Crop the face box with a small margin, resized to 112x112 for embedding."""
    H, W = bgr.shape[:2]
    mx, my = int(box.w * 0.15), int(box.h * 0.15)
    x0, y0 = max(0, box.x - mx), max(0, box.y - my)
    x1, y1 = min(W, box.x + box.w + mx), min(H, box.y + box.h + my)
    if x1 <= x0 or y1 <= y0:
        raise FaceImageError("Face box exceeds image bounds")
    crop = bgr[y0:y1, x0:x1]
    return cv2.resize(crop, (112, 112), interpolation=cv2.INTER_CUBIC)


def _quality_penalty(issues: list) -> float:
    """Confidence deduction from non-blocking quality issues."""
    if not issues:
        return 0.0
    degrading = [i for i in issues if i.severity == "degrading"]
    return min(QUALITY_CONF_PENALTY_MAX, 0.12 * len(issues) + 0.05 * len(degrading))


def _inconclusive(
    reasons: list[dict[str, str]],
    *,
    doc_face: dict[str, Any] | None,
    probe_face: dict[str, Any] | None,
    similarity: float | None = None,
    embedding_used: bool = False,
) -> dict[str, Any]:
    return {
        "face_detected_document": doc_face is not None,
        "face_detected_presented_person": probe_face is not None,
        "similarity_score": round(similarity, 4) if similarity is not None else None,
        "match_status": VERDICT_INCONCLUSIVE,
        "confidence": INCONCLUSIVE_CONFIDENCE,
        "verdict": VERDICT_INCONCLUSIVE,
        "similarity_match_threshold": SIMILARITY_MATCH_THRESHOLD,
        "similarity_no_match_max": SIMILARITY_NO_MATCH_MAX,
        "reasons": reasons,
        "warnings": [r["note"] for r in reasons],
        "one_to_one_only": True,
        "no_population_search": True,
        "embedding_backend_used": embedding_used,
    }


def _detect_best(bgr: Any) -> tuple[detection.FaceBox | None, list[dict[str, str]]]:
    """Detect faces and return (best valid face, safeguard reasons)."""
    reasons: list[dict[str, str]] = []
    if bgr is None or bgr.size == 0:
        reasons.append({"code": "image_unreadable", "note": "Image could not be decoded."})
        return None, reasons
    h, w = bgr.shape[:2]
    if min(h, w) < MIN_IMAGE_SIDE_PX:
        reasons.append({
            "code": "image_too_small",
            "note": f"Image resolution ({w}x{h}) too low for face verification.",
        })
        return None, reasons

    faces = detection.detect_faces(bgr)
    if not faces:
        reasons.append({"code": "no_face_detected", "note": "No face detected in the image."})
        return None, reasons

    best = faces[0]
    if len(faces) > 1:
        reasons.append({
            "code": "multiple_faces",
            "note": f"{len(faces)} faces detected; the most prominent one was used. "
                    "Multi-person images must be reviewed by a human.",
        })
    if best.w < MIN_FACE_SIDE_PX or best.h < MIN_FACE_SIDE_PX:
        reasons.append({
            "code": "face_too_small",
            "note": f"Detected face ({best.w}x{best.h}px) too small for a reliable embedding.",
        })
        return None, reasons
    if detection.extreme_pose(best):
        reasons.append({
            "code": "extreme_pose",
            "note": "Head pose too far from frontal for a meaningful comparison.",
        })
        return None, reasons
    if detection.occluded(best):
        reasons.append({
            "code": "face_occluded",
            "note": "Face appears occluded (covered or eyes closed).",
        })
        return None, reasons

    issues = evaluate_quality(bgr, best.box)
    blocking = [i for i in issues if i.severity == "blocking"]
    if blocking:
        reasons.extend({"code": i.code, "note": i.note} for i in blocking)
        return None, reasons
    best.quality_issues = issues
    return best, reasons


def verify(document_image_path: str, probe_image_path: str | None) -> dict[str, Any]:
    """Run one-to-one verification: document portrait vs presented person."""
    started = time.perf_counter()

    # --- embedding backend availability (honest abstention) ----------------
    model = select_model()
    if model is None:
        result = _inconclusive(
            [{
                "code": "model_unavailable",
                "note": "No face-embedding model is installed; run "
                        "scripts/get_face_models.py to fetch YuNet + SFace.",
            }],
            doc_face=None,
            probe_face=None,
        )
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    # --- inputs -------------------------------------------------------------
    try:
        doc_bgr = _load_bgr(document_image_path) if document_image_path else None
    except FaceImageError as exc:
        doc_bgr = None
        doc_load_error = str(exc)
    else:
        doc_load_error = None
    try:
        probe_bgr = _load_bgr(probe_image_path) if probe_image_path else None
    except FaceImageError as exc:
        probe_bgr = None
        probe_load_error = str(exc)
    else:
        probe_load_error = None

    if probe_bgr is None:
        reasons = [{
            "code": "no_presented_image",
            "note": "No live/presented person image was provided; one-to-one "
                    "verification needs both the document portrait and the presenter.",
        }]
        if probe_load_error:
            reasons = [{"code": "image_unreadable", "note": probe_load_error}]
        result = _inconclusive(reasons, doc_face=None, probe_face=None)
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    # --- probe frame ambiguity: who is the presenter? -----------------------
    # Several confident faces in the PROBE frame make any 1:1 verdict
    # meaningless (an impostor can stand beside the document holder), so the
    # comparison is refused outright and similarity is never reported. This
    # check runs BEFORE quality gating: ambiguity alone is disqualifying.
    all_probe_faces = detection.detect_faces(probe_bgr)
    if len(all_probe_faces) > MAX_PROBE_FACES:
        result = _inconclusive(
            [{
                "code": "probe_multiple_faces",
                "note": f"{len(all_probe_faces)} faces detected in the presented image "
                        "— it is ambiguous who is being verified; capture a "
                        "single-person frame and retry.",
            }],
            doc_face=None,
            probe_face=None,
        )
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    doc_face, doc_reasons = _detect_best(doc_bgr) if doc_bgr is not None else (None, [
        {"code": "image_unreadable", "note": doc_load_error or "Document image could not be decoded."}])
    probe_face, probe_reasons = _detect_best(probe_bgr)

    doc_face_dict = doc_face.to_dict() if doc_face else None
    probe_face_dict = probe_face.to_dict() if probe_face else None

    presentation_cues = _presentation_cues(probe_bgr, probe_face)

    if doc_face is None or probe_face is None:
        result = _inconclusive(
            doc_reasons + probe_reasons,
            doc_face=doc_face_dict,
            probe_face=probe_face_dict,
        )
        result["presentation_cues"] = presentation_cues
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    # --- embeddings + similarity ---------------------------------------------
    try:
        emb_doc = model.embed(_aligned_crop(doc_bgr, doc_face))
        emb_probe = model.embed(_aligned_crop(probe_bgr, probe_face))
    except Exception as exc:  # noqa: BLE001 - embedding failure = honest abstain
        logger.warning("Embedding failed: %s", exc)
        result = _inconclusive(
            [{"code": "embedding_failed", "note": f"Embedding computation failed: {exc}"}],
            doc_face=doc_face_dict,
            probe_face=probe_face_dict,
        )
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    if emb_doc is None or emb_probe is None:
        result = _inconclusive(
            [{"code": "embedding_failed", "note": "Embedding backend returned no vector."}],
            doc_face=doc_face_dict,
            probe_face=probe_face_dict,
        )
        result["presentation_cues"] = presentation_cues
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    similarity = cosine_similarity(emb_doc, emb_probe)

    # --- verdict --------------------------------------------------------------
    reasons = [r for r in (doc_reasons + probe_reasons) if r["code"] == "multiple_faces"]
    penalty = _quality_penalty(list(doc_face.quality_issues) + list(probe_face.quality_issues))

    if similarity >= SIMILARITY_MATCH_THRESHOLD:
        status = MATCH
        margin = similarity - SIMILARITY_MATCH_THRESHOLD
        confidence = min(STAGE_CONF_MAX, 0.82 + margin * 1.2 - penalty)
    elif similarity <= SIMILARITY_NO_MATCH_MAX:
        status = NO_MATCH
        margin = SIMILARITY_NO_MATCH_MAX - similarity
        confidence = min(STAGE_CONF_MAX, 0.82 + margin * 1.2 - penalty)
    else:
        status = VERDICT_INCONCLUSIVE
        confidence = INCONCLUSIVE_CONFIDENCE

    result = {
        "face_detected_document": True,
        "face_detected_presented_person": True,
        "presentation_cues": presentation_cues,
        "similarity_score": round(float(similarity), 4),
        "match_status": status,
        "confidence": round(float(max(0.0, min(1.0, confidence))), 3),
        "verdict": status,
        "similarity_match_threshold": SIMILARITY_MATCH_THRESHOLD,
        "similarity_no_match_max": SIMILARITY_NO_MATCH_MAX,
        "reasons": reasons,
        "warnings": [r["note"] for r in reasons],
        "document_face": doc_face_dict,
        "presented_face": probe_face_dict,
        "quality_issues": [
            i.to_dict()
            for i in list(doc_face.quality_issues) + list(probe_face.quality_issues)
        ],
        "embedding_backend": model.name,
        "one_to_one_only": True,
        "no_population_search": True,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "disclaimer": (
            "One-to-one verification only — NOT biometric identification against a "
            "population database. A similarity score is probabilistic evidence, never "
            "proof of identity; final decisions rest with authorized personnel."
        ),
    }
    return result
