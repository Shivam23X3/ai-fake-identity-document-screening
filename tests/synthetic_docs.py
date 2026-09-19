"""Step 13 — Synthetic specimen-document factory.

Generates clearly-artificial test documents for the failure-mode suite.
Every image carries a visible "SPECIMEN — NO VALUE" banner so nothing can
be mistaken for, or used as, a real identity document. All identity data is
fabricated (the same fictional UTO / UTOSLAND persons used by
scripts/make_samples). Nothing here copies or imitates any real person's
identity document.
"""
from __future__ import annotations

import io
import struct
import zlib

import cv2
import numpy as np

from app.services.ocr.mrz import mrz_check_digit  # noqa: E402
from scripts.make_samples import passport_mrz, visa_mrz  # noqa: E402


# ---------------------------------------------------------------------------
# Low-level encoders (stdlib, no file I/O)
# ---------------------------------------------------------------------------
def png_bytes(width: int = 64, height: int = 64, color=(30, 30, 30)) -> bytes:
    """A tiny valid PNG built with the stdlib only."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(color) * width
    raw = row * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def truncated_png_bytes() -> bytes:
    """A valid PNG header followed by a truncated IDAT — always fails decode."""
    ihdr = struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
    return header + b"\x00\x01\x02"  # no CRC, no data — corrupt by construction


def fake_image_bytes(real: bytes = b"GIF89a" + b"0" * 64) -> bytes:
    """Image payload that matches no allowed magic signature."""
    return real


def oversized_claim_png(target_mp: float = 120.0) -> bytes:
    """PNG header claiming far more pixels than the 50 MP decode budget."""
    side = int((target_mp * 1_000_000) ** 0.5)
    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", 0)
        + b"\x00"  # one stray byte then EOF — decode must fail fast
    )


# ---------------------------------------------------------------------------
# Degradation transforms (OpenCV, in-memory)
# ---------------------------------------------------------------------------
def to_jpeg_bytes(img: np.ndarray, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok
    return buf.tobytes()


def to_png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def blur_image(img: np.ndarray, ksize: int = 21) -> np.ndarray:
    return cv2.GaussianBlur(img, (ksize, ksize), 0)


def rotate_image(img: np.ndarray, degrees: float = 12.0) -> np.ndarray:
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(
        img, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=(240, 240, 240)
    )


def low_contrast(img: np.ndarray, alpha: float = 0.35) -> np.ndarray:
    return np.clip(img.astype(np.float32) * alpha + 60, 0, 255).astype(np.uint8)


def spliced_image(img: np.ndarray, seed: int = 11) -> np.ndarray:
    """Paste a high-noise rectangular patch — a classic splice indicator."""
    out = img.copy()
    rng = np.random.default_rng(seed)
    h, w = out.shape[:2]
    patch = rng.normal(150, 30, (max(40, h // 6), max(60, w // 5), 3)).astype(np.float32)
    patch = np.clip(patch, 0, 255).astype(np.uint8)
    y, x = h // 2, w // 8
    out[y : y + patch.shape[0], x : x + patch.shape[1]] = patch
    return out


def text_scrubbed_image(img: np.ndarray) -> np.ndarray:
    """Cover value areas with paper-colored rectangles (erased text)."""
    out = img.copy()
    h, w = out.shape[:2]
    for y0, y1 in ((180, 270), (300, 390)):
        if y1 < h:
            out[y0:y1, 40 : max(60, int(w * 0.55))] = (233, 227, 211)
    return out


# ---------------------------------------------------------------------------
# Specimen documents (fabricated identities only)
# ---------------------------------------------------------------------------
def _banner(img: np.ndarray) -> np.ndarray:
    cv2.putText(
        img, "SPECIMEN - NO VALUE", (18, img.shape[0] - 14),
        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (170, 30, 30), 2, cv2.LINE_AA,
    )
    return img


def specimen_passport(
    *, blur: float = 0.0, rotate: float = 0.0, contrast: float | None = None,
    expiry: str = "280415", scrub: bool = False, splice: bool = False,
    with_photo: bool = True,
) -> np.ndarray:
    """A synthetic passport page (machine-printed, MRZ with valid check
    digits). ``expiry`` is the MRZ YYMMDD date — pass e.g. '200101' for an
    expired document."""
    img = np.full((720, 1024, 3), 245, dtype=np.uint8)
    img[:] = (232, 226, 210)
    cv2.rectangle(img, (20, 20), (1003, 640), (120, 110, 90), 2)
    cv2.putText(img, "REPUBLIC OF UTOPIA - PASSPORT (SPECIMEN)", (60, 70),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (90, 70, 50), 2, cv2.LINE_AA)
    cv2.putText(img, "Surname", (60, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(img, "SPECIMEN", (200, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (15, 15, 15), 2, cv2.LINE_AA)
    cv2.putText(img, "Given Names", (60, 250), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(img, "TEST DOC", (200, 250), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (15, 15, 15), 2, cv2.LINE_AA)
    cv2.putText(img, "Passport No.", (560, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(img, "L898902C3", (700, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (15, 15, 15), 2, cv2.LINE_AA)
    cv2.putText(img, "Date of Expiry", (60, 370), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(img, "15 APR 2028" if expiry == "280415" else "01 JAN 2020", (200, 370),
                cv2.FONT_HERSHEY_SIMPLEX, 0.75, (15, 15, 15), 2, cv2.LINE_AA)
    if with_photo:
        cv2.rectangle(img, (760, 420), (960, 620), (200, 200, 200), -1)
        cv2.circle(img, (860, 500), 40, (150, 150, 150), -1)
        cv2.ellipse(img, (860, 590), (55, 40), 0, 0, 180, (150, 150, 150), -1)
    number = "L898902C3"
    dob = "740812"
    l1 = "P<UTOSPECIMEN<<TEST<DOC".ljust(44, "<")
    payload = (
        number + str(mrz_check_digit(number)) + "UTO"
        + dob + str(mrz_check_digit(dob)) + "F"
        + expiry + str(mrz_check_digit(expiry))
        + "Z184226B".ljust(14, "<") + str(mrz_check_digit("Z184226B".ljust(14, "<")))
    )
    l2 = payload + str(mrz_check_digit(payload))
    cv2.rectangle(img, (20, 645), (1003, 700), (250, 250, 250), -1)
    cv2.putText(img, l1, (35, 668), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (15, 15, 15), 2, cv2.LINE_AA)
    cv2.putText(img, l2, (35, 694), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (15, 15, 15), 2, cv2.LINE_AA)
    _banner(img)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    if rotate:
        M = cv2.getRotationMatrix2D((img.shape[1] / 2, img.shape[0] / 2), rotate, 1.0)
        img = cv2.warpAffine(img, M, (img.shape[1], img.shape[0]), borderValue=(240, 240, 240))
    if contrast is not None:
        img = low_contrast(img, contrast)
    if scrub:
        img = text_scrubbed_image(img)
    if splice:
        img = spliced_image(img)
    return img


def specimen_visa(**kwargs) -> np.ndarray:
    """A synthetic visa (MRV-B style MRZ). Accepts the same degradations."""
    img = np.full((560, 900, 3), 248, dtype=np.uint8)
    cv2.rectangle(img, (15, 15), (884, 460), (100, 90, 160), 2)
    cv2.putText(img, "UTOSLAND VISA (SPECIMEN)", (50, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 0.85, (60, 40, 130), 2, cv2.LINE_AA)
    cv2.putText(img, "Visa No.", (50, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(img, "AB9876543", (180, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (15, 15, 15), 2, cv2.LINE_AA)
    l1, l2 = visa_mrz()
    cv2.rectangle(img, (15, 460), (884, 545), (250, 250, 250), -1)
    cv2.putText(img, l1, (28, 488), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (15, 15, 15), 2, cv2.LINE_AA)
    cv2.putText(img, l2, (28, 514), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (15, 15, 15), 2, cv2.LINE_AA)
    _banner(img)
    if kwargs.get("blur"):
        img = cv2.GaussianBlur(img, (0, 0), kwargs["blur"])
    if kwargs.get("rotate"):
        M = cv2.getRotationMatrix2D((img.shape[1] / 2, img.shape[0] / 2), kwargs["rotate"], 1.0)
        img = cv2.warpAffine(img, M, (img.shape[1], img.shape[0]), borderValue=(240, 240, 240))
    if kwargs.get("contrast") is not None:
        img = low_contrast(img, kwargs["contrast"])
    if kwargs.get("scrub"):
        img = text_scrubbed_image(img)
    if kwargs.get("splice"):
        img = spliced_image(img)
    return img


def blank_page(w: int = 800, h: int = 560) -> np.ndarray:
    """A featureless page — OCR has nothing to read (OCR-failure case)."""
    img = np.full((h, w, 3), 246, dtype=np.uint8)
    return _banner(img)


def multi_face_scene(seed: int = 3) -> np.ndarray:
    """A 'document' with TWO face-like figures — the multiple-faces case."""
    rng = np.random.default_rng(seed)
    img = np.full((480, 640, 3), 205, dtype=np.uint8)

    def face(cx: int, cy: int) -> None:
        cv2.ellipse(img, (cx, cy), (95, 130), 0, 0, 360, (180, 160, 150), -1)
        cv2.circle(img, (cx - 35, cy - 35), 13, (50, 50, 70), -1)
        cv2.circle(img, (cx + 35, cy - 35), 13, (50, 50, 70), -1)
        cv2.ellipse(img, (cx, cy + 55), (40, 16), 0, 0, 180, (110, 90, 90), -1)

    face(200, 220)
    face(460, 220)
    noise = rng.normal(0, 4, img.shape).astype(np.int16)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
