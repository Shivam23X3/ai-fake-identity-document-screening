"""Step 14 — SIH demo-mode fixture generator.

Generates the four scripted demonstration scenarios as synthetic SPECIMEN
document images under ``data/demo_fixtures/``:

    CASE 1  valid document      -> case1_valid_passport.png
                                   + two probe selfies of the SAME (fictional) person
    CASE 2  tampered document   -> case2_tampered_passport.jpg  (spliced portrait +
                                   altered date of birth, JPEG re-save)
    CASE 3  identity mismatch   -> case3_document_passport.png  (Person A's document)
                                   + case3_probe_person_b.png   (Person B presenting it)
    CASE 4  poor quality        -> case4_blurred_passport.jpg   (heavy blur + low
                                   JPEG quality) + a sharp probe selfie

Every image carries a visible SPECIMEN / DEMO banner. All identity data is
fabricated (the fictional UTO / UTOSLAND personas used across this repo).
Nothing here is, or imitates, a real person's identity document, and nothing
here fakes a government verification.

Faces are drawn from one scale-proportional spec per person (all feature
offsets are fractions of face height), so the same fictional person renders
identically on the page portrait and on the probe selfie — while the two
persons differ in geometry, skin tone and hair. That makes the real SFace
1:1 engine score same-person pairs high and different-person pairs low.

Usage:
    .venv/Scripts/python scripts/make_demo_fixtures.py
    .venv/Scripts/python scripts/make_demo_fixtures.py --check-faces
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.make_samples import passport_mrz  # noqa: E402

OUT_DIR = ROOT / "data" / "demo_fixtures"

# --- canvas layout (mirrors scripts/make_samples.py so OCR behaves alike) ---
W, H = 1024, 720
PAGE_COLOR = (232, 226, 210)
# Single warm-brown ink family for ALL page furniture (header, border,
# labels, demo banner). Mixed cool hues (blue tags, red banners) make the
# hue histogram multi-modal and trip the tampering stage's multi-ink-hue
# stamp cue on an otherwise clean page (measured in the Step-14 calibration).
PAGE_INK = (50, 70, 90)   # BGR: warm brown (R > G > B)
# Faint guilloche wave color: uniform ~30 gray levels under PAGE_COLOR
# (232,226,210 → 202,196,180). Measured against the flat-patch detector:
# a 128px page tile needs Laplacian variance above ~1/8 of the text-tile
# median (~180 here) to avoid the "pasted flat patch" flag — a 1px AA line
# at Δ22 reaches only var≈44, while 2px AA at Δ30 clears it everywhere.
PAGE_GUILLOCHE = (202, 196, 180)   # BGR: PAGE_COLOR minus ~30 per channel
# Portrait box sized so YuNet reliably detects the in-page portrait (the
# default make_samples placeholder face is too small/soft for detection).
PORTRAIT_BOX = (750, 410, 980, 640)  # x0, y0, x1, y1
FACE_CENTER = (860, 520)
PAGE_FACE_HEIGHT = 150               # px; feature sizes scale from this

# Probe selfie canvas (landscape, like a webcam/web capture).
PROBE_SIZE = (640, 480)              # w, h
PROBE_FACE_HEIGHT = 240              # px; comfortably inside YuNet's sweet spot

# The two fictional demo persons. Feature geometry is defined as fractions of
# face height so the SAME person can be rendered at any scale (page portrait
# and probe selfie) with identical proportions — required for the real 1:1
# embedding compare to associate the two renderings. Geometry values are
# empirically calibrated against the YuNet detector (see --check-faces):
# these proportions detect reliably at both page and probe scale. The two
# persons differ in skin tone, hair and feature spacing so their SFace
# embeddings separate cleanly.
PERSON_A = {
    "label": "A",
    "skin": (120, 150, 190),         # BGR — cool, lighter tone
    "hair": (35, 30, 28),
    "rx_ratio": 0.379,               # head half-width / face height
    "eye_dx_ratio": 0.146,
    "eye_dy_ratio": -0.150,
    "eye_r_ratio": 0.042,
    "mouth_dy_ratio": 0.246,
    "mouth_w_ratio": 0.108,
    "seed": 41,
    "hair_arc": False,
    "brows": True,
}
PERSON_B = {
    "label": "B",
    "skin": (178, 148, 142),         # BGR — warm, darker tone
    "hair": (18, 16, 22),
    "rx_ratio": 0.379,
    "eye_dx_ratio": 0.190,           # wider-set eyes than A
    "eye_dy_ratio": -0.120,
    "eye_r_ratio": 0.055,           # larger eyes than A
    "mouth_dy_ratio": 0.230,
    "mouth_w_ratio": 0.150,
    "seed": 97,
    "hair_arc": True,
    "brows": True,
}
# Person B's probe selfie spec — CALIBRATED against the real YuNet+SFace
# stack (see scripts/_demo_face_lab.py and the Step-14 session log):
# - no hair arc (arcs regress YuNet's nose landmark past the pose gate)
# - GLASSES: visually distinct, detector-safe
# - face geometry (eye height / mouth height / nose offset) deliberately
#   different from A's: these shift the 5-landmark alignment warp, which is
#   the strongest detector-safe way to decorrelate the SFace embeddings.
# - a per-person FACE-REGION grain post-pass (seed 2000) finishes the
#   separation: measured A-vs-B similarity drops to ~0.20-0.23 (NO_MATCH,
#   ceiling 0.25) while A-vs-A keeps matching at ~0.90 (MATCH >= 0.363).
PERSON_B_PROBE = {
    **PERSON_B,
    "hair_arc": False,
    "eye_dy_ratio": -0.020,
    "mouth_dy_ratio": 0.140,
    "mouth_w_ratio": 0.110,
    "nose_dy_ratio": 0.060,
    "glasses": True,
    "iris_color": (80, 140, 200),   # amber-ish irises (BGR) — extra separation
    "face_grain_seed": 2000,
    "face_grain_sigma": 20.0,
}
# A's probe keeps the plain spec (no post-pass): same person must keep
# matching across page portrait and probe (measured ~0.90).
PERSON_A_PROBE = PERSON_A

# Post-render photo- realism recipe calibrated against YuNet (see
# scripts/_demo_face_lab.py): soft radial shading + a slight blur lift
# detector scores well above the 0.5 threshold at BOTH page and probe scale.
SOFTEN_BLUR_SIGMA = 0.8
SHADE_STRENGTH = 0.18

DOE_DISPLAY = "15 APR 2028"  # matches the MRZ expiry 280415 from passport_mrz()
DOB_DISPLAY_GENUINE = "12 AUG 1974"   # MRZ 740812 (Person A)
DOB_DISPLAY_TAMPERED = "21 AUG 1974"  # what the forger in Case 2 typed over


# ---------------------------------------------------------------------------
# Low-level drawing helpers
# ---------------------------------------------------------------------------
def _label(img, text, xy, scale=0.55, color=(40, 40, 40)):
    cv2.putText(img, text, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def _value(img, text, xy, scale=0.75, color=(15, 15, 15), thickness=2):
    cv2.putText(img, text, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _demo_banner(img) -> None:
    """The SIH demo honesty banner: a rule + label below the document header.

    Placed INSIDE the header block in the SAME ink hue as the other page
    furniture: full-width rows above the header reproduce at two vertical
    offsets and trip the copy-move detector, and extra saturated hues trip
    the multi-ink-hue "stamped mark" cue (both measured in the Step-14
    calibration)."""
    cv2.line(img, (40, 108), (980, 108), PAGE_INK, 2)
    _label(
        img, "SIH 2026 DEMONSTRATION - SYNTHETIC DATA - NOT A REAL DOCUMENT",
        (60, 104), 0.5, PAGE_INK,
    )


def _guilloche(img: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> None:
    """Faint security-pattern waves across the page middle.

    Any passport layout with a large empty center gets ≥2 tiles at ~zero
    Laplacian variance vs the text-tile median — the tampering stage's
    flat-patch cue then fires (this is visible even on the repo's own
    sample scans). A low-contrast wavy-line pattern gives every tile real
    texture without hurting OCR contrast. Drawn UNDER the text blocks.
    """
    rng = np.random.default_rng(7)
    for row in range(y0, y1, 6):
        amp = 4.0 + float(rng.uniform(0, 6))
        phase = float(rng.uniform(0, 6.283))
        freq = 0.020 + float(rng.uniform(0, 0.012))
        xs = np.arange(x0, x1)
        ys = (row + amp * np.sin(freq * xs + phase)).astype(int)
        pts = np.stack([xs, ys], axis=1).reshape(-1, 1, 2)
        cv2.polylines(img, [pts], False, PAGE_GUILLOCHE, 2, cv2.LINE_AA)


def _patch(img, box, color) -> None:
    x0, y0, x1, y1 = box
    img[y0:y1, x0:x1] = color


def _grain(img, seed: int, sigma: float) -> np.ndarray:
    """Monochrome (luminance-only) scanner grain.

    Per-channel RGB noise scatters hues across the page and trips the
    tampering stage's multi-ink-hue stamp cue (4.5% of pixels land above the
    0.25 saturation floor with random hues). Equal noise on all channels
    keeps saturation nearly constant while still flattening empty tiles.
    """
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, sigma, img.shape[:2]).astype(np.float32)
    return np.clip(img.astype(np.float32) + noise[..., None], 0, 255).astype(np.uint8)


def _draw_face(img: np.ndarray, cx: int, cy: int, spec: dict, face_height: int) -> None:
    """Draw one fictional 'photo of a face'. Every feature offset is a fixed
    fraction of ``face_height`` so the same spec renders identically at any
    scale (page portrait vs probe selfie)."""
    ry = max(1, face_height // 2)
    rx = max(1, round(spec["rx_ratio"] * face_height))
    eye_dx = round(spec["eye_dx_ratio"] * face_height)
    eye_dy = round(spec["eye_dy_ratio"] * face_height)
    eye_r = max(2, round(spec["eye_r_ratio"] * face_height))
    mouth_dy = round(spec["mouth_dy_ratio"] * face_height)
    mouth_w = max(3, round(spec["mouth_w_ratio"] * face_height))
    # Nose offset defaults to the historical fixed 6 px; specs may override
    # (face-length is a powerful, detector-safe structural differentiator).
    nose_dy = round(spec["nose_dy_ratio"] * face_height) if "nose_dy_ratio" in spec else 6

    # Head + neck
    cv2.ellipse(img, (cx, cy), (rx, ry), 0, 0, 360, spec["skin"], -1)
    cv2.rectangle(img, (cx - rx // 3, cy + ry - 8), (cx + rx // 3, cy + ry + 22),
                  spec["skin"], -1)
    if spec.get("hair_arc"):
        cv2.ellipse(img, (cx, cy - ry // 2), (rx + 6, ry // 2 + 8), 0, 180, 360,
                    spec["hair"], -1)
    # Brows (lift detector confidence), then eyes, nose hint, mouth
    if spec.get("brows"):
        bw, bt = int(eye_dx * 0.9), max(2, eye_r // 2)
        for sgn in (-1, 1):
            cv2.rectangle(
                img,
                (cx + sgn * eye_dx - bw, cy + eye_dy - eye_r - 4 - bt),
                (cx + sgn * eye_dx + bw, cy + eye_dy - eye_r - 4),
                spec["hair"], -1,
            )
    # Sclera (bright eye whites) — adds realistic bright structure around the
    # pupils; changes the SFace embedding materially while HELPING detection.
    if spec.get("sclera"):
        for sgn in (-1, 1):
            cv2.ellipse(
                img,
                (cx + sgn * eye_dx, cy + eye_dy),
                (int(eye_r * 1.9), int(eye_r * 1.3)),
                0, 0, 360, (235, 232, 228), -1,
            )
    # Glasses (Person B's probe look) — rims around each eye + bridge.
    if spec.get("glasses"):
        rim_r = eye_r + max(4, face_height // 30)
        for sgn in (-1, 1):
            cv2.circle(img, (cx + sgn * eye_dx, cy + eye_dy), rim_r, (60, 55, 50), 2)
        cv2.line(img, (cx - eye_dx + rim_r, cy + eye_dy),
                 (cx + eye_dx - rim_r, cy + eye_dy), (60, 55, 50), 2)
    for sgn in (-1, 1):
        cv2.circle(img, (cx + sgn * eye_dx, cy + eye_dy), eye_r, (40, 40, 45), -1)
    nose_c = tuple(int(c * 0.8) for c in spec["skin"])
    cv2.ellipse(img, (cx, cy + nose_dy), (max(3, face_height // 22), max(4, face_height // 14)),
                0, 0, 360, nose_c, -1)
    cv2.ellipse(img, (cx, cy + mouth_dy), (mouth_w, max(3, face_height // 24)), 0, 0, 180,
                (95, 85, 85), -1)


def _soften(img: np.ndarray, *, center: tuple[float, float] | None = None) -> np.ndarray:
    """Photo-realism post-pass: soft radial shading + slight blur.

    Calibrated against the YuNet detector: this lifts detection scores well
    above threshold on both page portraits and probe selfies (flat renders
    sit too close to the decision boundary)."""
    h, w = img.shape[:2]
    cx, cy = center if center else (w / 2.0, h * 0.52)
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt(((xx - cx) / (w / 4.0)) ** 2 + ((yy - cy) / (h / 2.4)) ** 2)
    factor = np.clip(1.0 - SHADE_STRENGTH * np.clip(d - 0.55, 0, None), 0.7, 1.0)
    img = np.clip(img.astype(np.float32) * factor[..., None], 0, 255).astype(np.uint8)
    return cv2.GaussianBlur(img, (0, 0), SOFTEN_BLUR_SIGMA)


def _probe_image(spec: dict, *, brightness: float = 1.0, out_size=PROBE_SIZE) -> np.ndarray:
    """A selfie-style probe image: the person's face rendered directly at
    probe scale on a plain studio-like background. Rendered at full scale
    rather than cropped from the page so detection is reliable."""
    w, h = out_size
    img = np.full((h, w, 3), 205, dtype=np.uint8)
    _draw_face(img, w // 2, int(h * 0.52), spec, PROBE_FACE_HEIGHT)
    img = _soften(img, center=(w / 2.0, h * 0.52))
    if brightness != 1.0:
        img = np.clip(img.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    return _grain(img, spec["seed"] + 7, 2.0)


def _face_region_grain(img: np.ndarray, seed: int, sigma: float) -> np.ndarray:
    """Grain ONLY the face region (centered on the standard probe face spot).

    Per-person texture decorrelates the SFace embedding between the two
    fictional persons without touching detector landmarks outside the face.
    """
    fh = PROBE_FACE_HEIGHT
    cx, cy = PROBE_SIZE[0] // 2, int(PROBE_SIZE[1] * 0.52)
    mask = np.zeros(img.shape[:2], np.uint8)
    cv2.ellipse(mask, (cx, cy), (int(fh * 0.42), int(fh * 0.55)), 0, 0, 360, 255, -1)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, sigma, img.shape).astype(np.float32)
    out = img.astype(np.float32) + noise * (mask.astype(np.float32) / 255.0)[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def _person_b_probe_post(img: np.ndarray) -> np.ndarray:
    """Identity-separation post-pass for Person B's probe selfie.

    Repaints the irises amber at the YuNet-reported eye landmarks (both a
    visual differentiator and an embedding differentiator), then applies the
    per-person face-region grain. Calibrated end-to-end: the generated
    fixture measures NO_MATCH (~0.20) against A's document portrait while
    A's own probe keeps MATCH (~0.90) — see --check-faces.
    """
    from app.services.face import detection  # local import: generator-only dep

    faces = detection.detect_faces(img)
    if not faces or faces[0].right_eye is None or faces[0].left_eye is None:
        raise RuntimeError(
            "Person B probe post-pass failed: YuNet could not find the face/eyes. "
            "Run scripts/get_face_models.py first."
        )
    box = faces[0]
    iris_color = PERSON_B_PROBE.get("iris_color", (80, 140, 200))
    eye_r = max(3, round(PERSON_B_PROBE["eye_r_ratio"] * PROBE_FACE_HEIGHT))
    for (ex, ey) in (box.right_eye, box.left_eye):
        cv2.circle(img, (ex, ey), eye_r + 2, iris_color, -1)
        cv2.circle(img, (ex, ey), max(2, eye_r // 2), (30, 30, 35), -1)
    return _face_region_grain(
        img,
        PERSON_B_PROBE.get("face_grain_seed", 2000),
        PERSON_B_PROBE.get("face_grain_sigma", 16.0),
    )


# ---------------------------------------------------------------------------
# The base (genuine) synthetic passport used by every case
# ---------------------------------------------------------------------------
def _soften_region(img: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    """Apply the photo-realism soften pass to ONE region only.

    The global soften+grain recipe blurs the 0.62-scale MRZ lines beyond
    RapidOCR's line joiner (measured: MRZ unparseable, 3 fields). OCR needs
    crisp machine print; only the portrait area needs photo-like shading.
    """
    x0, y0, x1, y1 = box
    pad = 30
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    h, w = img.shape[:2]
    x1, y1 = min(w - 1, x1 + pad), min(h - 1, y1 + pad)
    region = img[y0:y1, x0:x1]
    rh, rw = region.shape[:2]
    yy, xx = np.mgrid[0:rh, 0:rw]
    d = np.sqrt(((xx - rw / 2.0) / (rw / 2.0)) ** 2 + ((yy - rh / 2.0) / (rh / 2.0)) ** 2)
    factor = np.clip(1.0 - SHADE_STRENGTH * np.clip(d - 0.55, 0, None), 0.7, 1.0)
    region = np.clip(region.astype(np.float32) * factor[..., None], 0, 255).astype(np.uint8)
    region = cv2.GaussianBlur(region, (0, 0), SOFTEN_BLUR_SIGMA)
    img[y0:y1, x0:x1] = region
    return img


def _base_passport(demo_tag: str) -> np.ndarray:
    img = np.full((H, W, 3), 245, dtype=np.uint8)
    img[:] = PAGE_COLOR
    # Faint security-pattern waves UNDER everything else: gives every page
    # tile real texture so the empty middle never reads as a "pasted flat
    # patch" (the repo's own valid sample flags there without it).
    _guilloche(img, 30, 130, W - 30, 625)
    cv2.rectangle(img, (20, 20), (W - 21, H - 21), PAGE_INK, 2)
    _value(img, "REPUBLIC OF UTOPIA - PASSPORT", (60, 60), 0.8, PAGE_INK)
    _demo_banner(img)
    # Tag in the same warm ink family as all other furniture (see PAGE_INK).
    _label(img, demo_tag, (42, 122), 0.5, PAGE_INK)

    _label(img, "Type / Code / Country", (60, 150))
    _value(img, "P", (60, 180), 0.6)
    _value(img, "UTO", (140, 180), 0.6)
    _label(img, "Surname", (60, 230))
    _value(img, "SPECIMEN", (200, 230))
    _label(img, "Given Names", (60, 280))
    _value(img, "TEST DOC", (200, 280))
    _label(img, "Passport No.", (560, 230))
    _value(img, "L898902C3", (700, 230))
    _label(img, "Nationality", (560, 280))
    _value(img, "UTOPIAN", (700, 280))
    _label(img, "Date of Birth", (60, 340))
    _value(img, DOB_DISPLAY_GENUINE, (200, 340))
    _label(img, "Sex", (560, 340))
    _value(img, "F", (700, 340))
    _label(img, "Date of Expiry", (60, 400))
    _value(img, DOE_DISPLAY, (200, 400))
    _label(img, "Personal No.", (560, 400))
    _value(img, "Z184226B", (700, 400))

    # Photo placeholder + the genuine holder's portrait (Person A).
    x0, y0, x1, y1 = PORTRAIT_BOX
    cv2.rectangle(img, (x0, y0), (x1, y1), (200, 200, 200), -1)
    _draw_face(img, FACE_CENTER[0], FACE_CENTER[1], PERSON_A, PAGE_FACE_HEIGHT)

    # MRZ with valid ICAO check digits (same persona as scripts/make_samples).
    # Layout measured against RapidOCR: the MRZ box must end well above the
    # bottom SPECIMEN banner — banner glyphs that share the white box merge
    # with line 2 and break MRZ detection entirely (conf 0.83 / no MRZ).
    l1, l2 = passport_mrz()
    cv2.rectangle(img, (20, 628), (W - 21, 688), (250, 250, 250), -1)
    # 0.66 (not 0.62): at 0.62 RapidOCR merges one '<' pair on line 2 (43
    # chars) → composite check fails; ≥0.66 segments all 44 reliably.
    _value(img, l1, (35, 652), 0.66)
    _value(img, l2, (35, 678), 0.66)

    # SPECIMEN banner bottom — same warm ink family as the rest of the page
    # (a saturated red banner feeds the multi-ink-hue stamp cue).
    cv2.putText(
        img, "SPECIMEN - NO VALUE", (18, H - 8), cv2.FONT_HERSHEY_SIMPLEX,
        0.6, PAGE_INK, 2, cv2.LINE_AA,
    )
    # CRISP machine-printed page, matching tests/synthetic_docs.py and
    # data/samples/sample_passport.png (the tampering-calibrated baseline).
    # Measured in the Step-14 calibration (see data/demo_fixtures/README.md):
    # - global grain/blur breaks MRZ OCR or turns the dense MRZ text tiles
    #   into noise outliers (z >= 4 against a tiny robust spread);
    # - a locally softened portrait becomes a noise/sharpness outlier;
    # - the crisp page reads "inconclusive" (one low flat-tile cue) exactly
    #   like the repo's own valid sample — an honest, non-suspicious baseline.
    return img


def _tamper_case2(base: np.ndarray) -> np.ndarray:
    """Splice a DIFFERENT portrait over the genuine one and alter the printed
    date of birth, then JPEG re-save (the classic 'edited scan' history)."""
    img = base.copy()
    # 1. Portrait substitution: patch the photo area, draw Person B over it.
    # (On the crisp-page recipe the splice cue is mainly the JPEG re-save
    # metadata; the DOB edit below is the visible printed-content anomaly.)
    _patch(img, PORTRAIT_BOX, (200, 200, 200))
    _draw_face(img, FACE_CENTER[0], FACE_CENTER[1], PERSON_B, PAGE_FACE_HEIGHT)
    # 2. Printed DOB alteration (value area only; MRZ untouched -> a visible
    #    VZ/MRZ discrepancy for the reviewer to catch).
    _patch(img, (192, 316, 420, 348), PAGE_COLOR)
    _value(img, DOB_DISPLAY_TAMPERED, (200, 340))
    return img


# ---------------------------------------------------------------------------
# Manifest (the scripted case definitions consumed by /api/demo + UI)
# ---------------------------------------------------------------------------
def build_manifest() -> dict:
    def doc(name: str, note: str) -> dict:
        return {"filename": name, "doc_type_hint": "passport", "note": note}

    return {
        "notice": (
            "DEMONSTRATION / SIMULATED DATA ONLY. Every fixture here is a "
            "synthetic specimen generated by scripts/make_demo_fixtures.py "
            "with fabricated identities (UTO / UTOSLAND). Nothing is a real "
            "document and nothing simulates a government verification."
        ),
        "generator": "scripts/make_demo_fixtures.py (Step 14)",
        "cases": {
            "case1_valid": {
                "title": "CASE 1 - Valid document",
                "storyline": (
                    "A genuine (fictional) UTOPIA passport of Person A is screened "
                    "with a matching live photo of Person A."
                ),
                "expected": [
                    "OCR successful",
                    "Validation passes",
                    "No obvious tampering",
                    "Face match",
                    "Low risk",
                    "Audit recorded",
                ],
                "document": doc("case1_valid_passport.png", "Genuine specimen passport (Person A)."),
                "probe": doc("case1_probe_person_a_1.png", "Live photo of Person A (matches the portrait)."),
                "probe_alternate": doc("case1_probe_person_a_2.png", "Second live photo of Person A (brighter reframe)."),
            },
            "case2_tampered": {
                "title": "CASE 2 - Tampered document",
                "storyline": (
                    "The same passport after tampering: a different portrait was "
                    "spliced in and the printed date of birth was altered, then the "
                    "page was re-saved as JPEG."
                ),
                "expected": [
                    "OCR successful",
                    "Validation may pass",
                    "Tampering indicators detected",
                    "Risk increased",
                    "Human review required",
                    "Audit recorded",
                ],
                "document": doc("case2_tampered_passport.jpg", "Spliced portrait + altered DOB, JPEG re-save."),
                "probe": doc(
                    "case1_probe_person_a_1.png",
                    "Live photo of the (original) holder. NOTE: the similarity-based "
                    "1:1 face compare can still MATCH a look-alike synthetic portrait "
                    "- the forensic tampering stage is what catches this case, which "
                    "is exactly the defense-in-depth lesson of Case 2.",
                ),
            },
            "case3_identity_mismatch": {
                "title": "CASE 3 - Identity mismatch",
                "storyline": (
                    "An untampered document of Person A is presented together with a "
                    "live photo of a DIFFERENT person (Person B): document fine, "
                    "holder wrong."
                ),
                "expected": [
                    "Document information extracted",
                    "Document validation passes",
                    "Face verification fails",
                    "High risk",
                    "Human review required",
                    "Audit recorded",
                ],
                "document": doc("case3_document_passport.png", "Untampered specimen passport (Person A)."),
                "probe": doc("case3_probe_person_b.png", "Live photo of Person B (NOT the document holder)."),
            },
            "case4_low_quality": {
                "title": "CASE 4 - Poor quality document",
                "storyline": (
                    "The genuine passport photographed badly: motion blur and heavy "
                    "JPEG compression. The pipeline must degrade honestly instead of "
                    "guessing."
                ),
                "expected": [
                    "Low OCR confidence",
                    "Inconclusive validation where appropriate",
                    "Human review required",
                ],
                "document": doc("case4_blurred_passport.jpg", "Blurred, low-quality JPEG scan."),
                "probe": doc("case1_probe_person_a_1.png", "Sharp live photo of Person A (the presenter is fine - the scan is not)."),
            },
        },
    }


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def generate(out_dir: Path = OUT_DIR, check_faces: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _base_passport("DEMO CASE 1 / 2 - SCRIPTED SCENARIO")
    case3 = _base_passport("DEMO CASE 3 - SCRIPTED SCENARIO")
    case2 = _tamper_case2(base)
    case4 = cv2.GaussianBlur(base, (0, 0), 2.6)

    images = {
        "case1_valid_passport.png": base,
        "case2_tampered_passport.jpg": case2,
        "case3_document_passport.png": case3,
        "case4_blurred_passport.jpg": case4,
        "case1_probe_person_a_1.png": _probe_image(PERSON_A),
        "case1_probe_person_a_2.png": _probe_image(PERSON_A, brightness=1.06),
        "case3_probe_person_b.png": _person_b_probe_post(_probe_image(PERSON_B_PROBE)),
    }
    for name, img in images.items():
        path = out_dir / name
        if not cv2.imwrite(str(path), img):
            raise RuntimeError(f"cv2.imwrite failed for {path}")

    manifest = build_manifest()
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # README honesty banner (same convention as data/samples).
    (out_dir / "README.md").write_text(
        "# DEMO FIXTURES - SIH 2026 DEMONSTRATION MODE (Step 14)\n\n"
        "**Everything in this folder is FAKE, synthetic, demonstration data**\n"
        "generated by `scripts/make_demo_fixtures.py` (see `manifest.json`).\n\n"
        "- All documents are clearly-bannered SPECIMEN images with fabricated\n"
        "  identities (the fictional UTO / UTOSLAND personas). No real identity\n"
        "  document or real personal data is used anywhere.\n"
        "- Demo results are labeled DEMONSTRATION/SIMULATED in the API and UI.\n"
        "- This mode never fakes real government verification: registry lookups\n"
        "  stay MOCK-stamped, the face module is a real 1:1 embedding compare,\n"
        "  and every pipeline stage runs exactly as in production.\n\n"
        "## The four scripted cases\n\n"
        "1. **case1_valid** - valid document: OCR ok, validation passes, no\n"
        "   obvious tampering, face MATCH, low risk, audit recorded.\n"
        "2. **case2_tampered** - spliced portrait + altered printed DOB:\n"
        "   tampering indicators, increased risk, human review, audit recorded.\n"
        "3. **case3_identity_mismatch** - genuine document + DIFFERENT presenter:\n"
        "   validation passes, face verification fails, high risk, review.\n"
        "4. **case4_low_quality** - blurred/low-quality scan: low OCR confidence,\n"
        "   inconclusive where appropriate, human review.\n\n"
        "## Fixture calibration notes (Step 14)\n\n"
        "These images are calibrated end-to-end against the real modules so the\n"
        "scripted outcomes reproduce reliably (measure with --check-faces):\n\n"
        "- **Face fixtures.** A's probe matches A's page portrait at similarity\n"
        "  ~0.86 (MATCH threshold 0.363); B's probe scores ~0.24 against it\n"
        "  (NO_MATCH ceiling 0.25) via structural geometry differences plus a\n"
        "  per-person face-region grain post-pass and repainted irises.\n"
        "- **MRZ.** Printed at 0.66 scale: at 0.62 RapidOCR merges one '<' pair\n"
        "  (43 chars) and the composite check digit fails. The MRZ box ends well\n"
        "  above the bottom SPECIMEN banner (shared box glyphs merge lines).\n"
        "- **Page look.** Crisp machine print in one warm-brown ink family\n"
        "  (PAGE_INK) with a faint guilloche wave pattern (PAGE_GUILLOCHE,\n"
        "  ~30 gray levels under the page). Measured effects: mixed hues trip\n"
        "  the stamp ink cue; without the guilloche the empty page middle\n"
        "  flags as flat 'pasted patch' tiles; global grain/blur breaks MRZ\n"
        "  OCR or creates noise outliers; a locally softened portrait becomes\n"
        "  a noise/sharpness outlier. The calibrated page reads\n"
        "  no_obvious_manipulation (risk ~4) like the repo's own valid sample.\n"
        "- **Case 2 lesson.** The spliced synthetic portrait can still\n"
        "  similarity-MATCH A's probe (SFace keys on coarse structure) — the\n"
        "  forensic tampering stage (JPEG metadata + DOB-patch ink cue) is\n"
        "  what catches this case. Defense in depth, honestly labeled.\n",
        encoding="utf-8",
    )

    print(f"Wrote {len(images)} fixture images + manifest.json + README.md to {out_dir}")

    if check_faces:
        _check_faces(out_dir)


def _check_faces(out_dir: Path) -> None:
    """Print measured 1:1 face verdicts between the fixtures (best effort)."""
    try:
        from app.services.face.engine import verify
    except Exception as exc:  # noqa: BLE001
        print(f"(face check unavailable: {exc})")
        return
    doc1 = str(out_dir / "case1_valid_passport.png")
    doc3 = str(out_dir / "case3_document_passport.png")
    pairs = [
        ("case1 doc vs Person A probe (expect MATCH)", doc1, "case1_probe_person_a_1.png"),
        ("case1 doc vs Person B probe (expect NOT MATCH)", doc1, "case3_probe_person_b.png"),
        ("case3 doc vs Person B probe (expect NOT MATCH)", doc3, "case3_probe_person_b.png"),
    ]
    for note, doc, probe in pairs:
        try:
            res = verify(doc, str(out_dir / probe))
            print(
                f"{note}: {res['match_status']} "
                f"similarity={res['similarity_score']} confidence={res['confidence']}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{note}: check failed: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SIH demo-mode fixtures (Step 14).")
    parser.add_argument(
        "--check-faces", action="store_true",
        help="After generating, run the real face engine across fixture pairs and print verdicts.",
    )
    args = parser.parse_args()
    generate(check_faces=args.check_faces)
