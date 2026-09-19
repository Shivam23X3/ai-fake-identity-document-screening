"""Fetch the face-verification models (Step 7) into ``models/``.

Downloads the OpenCV Zoo ONNX weights used by the face module:

- ``face_detection_yunet_2023mar.onnx``  — YuNet face detector (~230 KB)
- ``face_recognition_sface_2021dec.onnx`` — SFace embedding network (~37 MB)

    .venv/Scripts/python scripts/get_face_models.py

Idempotent: files already present (and non-trivially sized) are skipped.
Models are gitignored — never commit them.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"

FILES: dict[str, tuple[str, int]] = {
    "face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        100_000,
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        10_000_000,
    ),
}


def fetch(name: str, url: str, min_bytes: int) -> bool:
    dest = MODELS_DIR / name
    if dest.is_file() and dest.stat().st_size >= min_bytes:
        print(f"  [skip] {name} (already present, {dest.stat().st_size:,} bytes)")
        return True
    print(f"  [get ] {name} ...")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as out:
            while chunk := resp.read(1 << 16):
                out.write(chunk)
    except Exception as exc:  # noqa: BLE001 - network errors are user-facing
        print(f"  [FAIL] {name}: {exc}")
        dest.unlink(missing_ok=True)
        return False
    size = dest.stat().st_size
    if size < min_bytes:
        print(f"  [FAIL] {name}: downloaded {size:,} bytes, expected >= {min_bytes:,}")
        dest.unlink(missing_ok=True)
        return False
    print(f"  [ok  ] {name} ({size:,} bytes)")
    return True


def all_models_present() -> bool:
    """True when every model exists locally (used by tests/docs)."""
    return all(
        (MODELS_DIR / name).is_file() and (MODELS_DIR / name).stat().st_size >= min_bytes
        for name, (_url, min_bytes) in FILES.items()
    )


def main() -> int:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Face models directory: {MODELS_DIR}")
    ok = all(fetch(name, url, min_bytes) for name, (url, min_bytes) in FILES.items())
    if ok:
        print("All face models ready — face verification is active.")
    else:
        print("Some models are missing: the face stage will honestly report "
              "'not implemented' until they are downloaded.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
