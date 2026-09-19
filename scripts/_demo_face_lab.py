"""TEMP experiment: calibrate synthetic faces for YuNet at page+probe scales."""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.face import detection  # noqa: E402

W, H = 1024, 720
PORTRAIT_BOX = (750, 410, 980, 640)
FACE_CENTER = (860, 520)


def draw_face(img, cx, cy, spec, face_height):
    ry = max(1, face_height // 2)
    rx = max(1, round(spec["rx_ratio"] * face_height))
    eye_dx = round(spec["eye_dx_ratio"] * face_height)
    eye_dy = round(spec["eye_dy_ratio"] * face_height)
    eye_r = max(2, round(spec["eye_r_ratio"] * face_height))
    mouth_dy = round(spec["mouth_dy_ratio"] * face_height)
    mouth_w = max(3, round(spec["mouth_w_ratio"] * face_height))
    skin = spec["skin"]

    cv2.ellipse(img, (cx, cy), (rx, ry), 0, 0, 360, skin, -1)
    cv2.rectangle(img, (cx - rx // 3, cy + ry - 8), (cx + rx // 3, cy + ry + 22), skin, -1)
    if spec.get("hair_arc"):
        cv2.ellipse(img, (cx, cy - ry // 2), (rx + 6, ry // 2 + 8), 0, 180, 360, spec["hair"], -1)
    if spec.get("brows"):
        bw, bt = int(eye_dx * 0.9), max(2, eye_r // 2)
        for sgn in (-1, 1):
            cv2.rectangle(img, (cx + sgn * eye_dx - bw, cy + eye_dy - eye_r - 4 - bt),
                          (cx + sgn * eye_dx + bw, cy + eye_dy - eye_r - 4), spec["hair"], -1)
    for sgn in (-1, 1):
        if spec.get("sclera"):
            cv2.circle(img, (cx + sgn * eye_dx, cy + eye_dy), eye_r + max(2, eye_r // 2), (235, 235, 230), -1)
        cv2.circle(img, (cx + sgn * eye_dx, cy + eye_dy), eye_r, (40, 40, 45), -1)
    if spec.get("nose", True):
        nc = tuple(int(c * 0.8) for c in skin)
        cv2.ellipse(img, (cx, cy + 6), (max(3, face_height // 22), max(4, face_height // 14)), 0, 0, 360, nc, -1)
    cv2.ellipse(img, (cx, cy + mouth_dy), (mouth_w, max(3, face_height // 24)), 0, 0, 180, (95, 85, 85), -1)


def page(spec, grain_sigma=2.0):
    img = np.full((H, W, 3), 245, np.uint8)
    img[:] = (232, 226, 210)
    cv2.rectangle(img, (20, 20), (W - 21, H - 21), (120, 110, 90), 2)
    cv2.putText(img, "REPUBLIC OF UTOPIA - PASSPORT", (60, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (90, 70, 50), 2, cv2.LINE_AA)
    cv2.line(img, (40, 80), (980, 80), (60, 60, 170), 2)
    cv2.putText(img, "SIH 2026 DEMONSTRATION - SYNTHETIC DATA - NOT A REAL DOCUMENT", (42, 102), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (60, 60, 170), 1, cv2.LINE_AA)
    cv2.putText(img, "Surname", (60, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(img, "SPECIMEN", (200, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (15, 15, 15), 2, cv2.LINE_AA)
    x0, y0, x1, y1 = PORTRAIT_BOX
    cv2.rectangle(img, (x0, y0), (x1, y1), (200, 200, 200), -1)
    draw_face(img, FACE_CENTER[0], FACE_CENTER[1], spec, 150)
    cv2.rectangle(img, (20, 645), (W - 21, 705), (250, 250, 250), -1)
    cv2.putText(img, "P<UTOSPECIMEN<<TEST<DOC".ljust(44, "<"), (35, 668), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (15, 15, 15), 2, cv2.LINE_AA)
    cv2.putText(img, "L898902C36UTO7408129F2804159Z184226B<<<<<94", (35, 694), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (15, 15, 15), 2, cv2.LINE_AA)
    rng = np.random.default_rng(2026)
    noise = rng.normal(0, grain_sigma, img.shape).astype(np.float32)
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def probe(spec, brightness=1.0, blur=0.0, shade=False):
    img = np.full((480, 640, 3), 205, np.uint8)
    draw_face(img, 320, 249, spec, 240)
    if shade:
        # soft radial shading: darker toward the head-ellipse edge (photo-like)
        h, w = img.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        d = np.sqrt(((xx - 320) / 160.0) ** 2 + ((yy - 249) / 200.0) ** 2)
        factor = np.clip(1.0 - 0.18 * np.clip(d - 0.55, 0, None), 0.7, 1.0)
        img = np.clip(img.astype(np.float32) * factor[..., None], 0, 255).astype(np.uint8)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    if brightness != 1.0:
        img = np.clip(img.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    rng = np.random.default_rng(spec["seed"] + 7)
    noise = rng.normal(0, 2.0, img.shape).astype(np.float32)
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


BASE_A = {"skin": (120, 150, 190), "hair": (35, 30, 28), "rx_ratio": 0.379,
          "eye_dx_ratio": 0.146, "eye_dy_ratio": -0.150, "eye_r_ratio": 0.042,
          "mouth_dy_ratio": 0.246, "mouth_w_ratio": 0.108, "seed": 41, "hair_arc": False}
BASE_B = {"skin": (178, 148, 142), "hair": (18, 16, 22), "rx_ratio": 0.379,
          "eye_dx_ratio": 0.190, "eye_dy_ratio": -0.120, "eye_r_ratio": 0.055,
          "mouth_dy_ratio": 0.230, "mouth_w_ratio": 0.150, "seed": 97, "hair_arc": True}

variants = {
    "A base": BASE_A,
    "A blur0.8": BASE_A | {"_blur": 0.8},
    "A blur1.2": BASE_A | {"_blur": 1.2},
    "A shade": BASE_A | {"_shade": True},
    "A shade+blur0.8": BASE_A | {"_shade": True, "_blur": 0.8},
    "A shade+brows": BASE_A | {"_shade": True, "brows": True},
    "A shade+brows+blur0.8": BASE_A | {"_shade": True, "brows": True, "_blur": 0.8},
    "B base": BASE_B,
    "B blur0.8": BASE_B | {"_blur": 0.8},
    "B blur1.2": BASE_B | {"_blur": 1.2},
    "B shade": BASE_B | {"_shade": True},
    "B shade+blur0.8": BASE_B | {"_shade": True, "_blur": 0.8},
    "B shade+brows": BASE_B | {"_shade": True, "brows": True},
    "B shade+brows+blur0.8": BASE_B | {"_shade": True, "brows": True, "_blur": 0.8},
}


def apply_extras(img, spec):
    if spec.get("_shade"):
        h, w = img.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        d = np.sqrt(((xx - w / 2) / (w / 4.0)) ** 2 + ((yy - h * 0.52) / (h / 2.4)) ** 2)
        factor = np.clip(1.0 - 0.18 * np.clip(d - 0.55, 0, None), 0.7, 1.0)
        img = np.clip(img.astype(np.float32) * factor[..., None], 0, 255).astype(np.uint8)
    if spec.get("_blur"):
        img = cv2.GaussianBlur(img, (0, 0), spec["_blur"])
    return img


for name, spec in variants.items():
    p = apply_extras(probe(spec), spec)
    pg = apply_extras(page(spec), {k: v for k, v in spec.items() if k.startswith("_")})
    fp = detection.detect_faces(p)
    fg = detection.detect_faces(pg)
    s_p = f"{fp[0].w}x{fp[0].h}@{fp[0].score:.2f}" if fp else "-"
    s_g = f"{fg[0].w}x{fg[0].h}@{fg[0].score:.2f}" if fg else "-"
    print(f"{name:24s} probe: {s_p:20s} page: {s_g}")
