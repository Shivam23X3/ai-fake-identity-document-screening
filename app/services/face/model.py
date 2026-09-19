"""Embedding backend interface for face verification.

Default backend: **SFace** (OpenCV Zoo, ArcFace-style training) via
``cv2.FaceRecognizerSF`` — 512-d embeddings, cosine similarity, CPU, fully
offline. A deployment can register a stronger ArcFace ONNX model instead:

    from app.services.face.model import register_embedding_model

    class MyArcFace:
        name = "glint360k-arcface-r50"
        def available(self) -> bool: ...
        def embed(self, aligned_bgr_112) -> np.ndarray:  # 512-d float32
            ...

    register_embedding_model(MyArcFace())

Select via ``APP_FACE_EMBEDDING_MODEL=<name>`` (``sface`` is the built-in
default, ``none`` abstains). Backends are lazy: importing this module never
loads weights, and failures degrade to "unavailable", never a crash.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from app.core.config import get_settings
from app.services.face.constants import EMBEDDING_DIM, SFACE_FILENAME

logger = logging.getLogger(__name__)

_MODELS_DIR = Path(__file__).resolve().parents[3] / "models"
_sface: Any = None


class EmbeddingModel(Protocol):
    """Contract for an embedding backend (SFace or a custom ArcFace ONNX)."""

    name: str

    def available(self) -> bool: ...

    def embed(self, aligned_bgr: Any) -> Any:
        """Return a 512-d float32 embedding for an aligned 112x112 BGR face."""
        ...


def sface_model_path() -> Path:
    return _MODELS_DIR / SFACE_FILENAME


def _get_sface() -> Any:
    global _sface
    if _sface is None:
        path = sface_model_path()
        if not path.is_file():
            return None
        try:
            _sface = cv2.FaceRecognizerSF.create(str(path), "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("SFace failed to load: %s", exc)
            return None
    return _sface


class SFaceModel:
    """Built-in SFace backend via cv2.FaceRecognizerSF."""

    name = "sface"

    def available(self) -> bool:
        return _get_sface() is not None

    def embed(self, aligned_bgr: Any) -> Any:
        recognizer = _get_sface()
        if recognizer is None:
            return None
        img = aligned_bgr
        if img is None or img.size == 0:
            return None
        if img.shape[0] != 112 or img.shape[1] != 112:
            img = cv2.resize(img, (112, 112), interpolation=cv2.INTER_CUBIC)
        vec = recognizer.feature(img)
        return np.asarray(vec, dtype=np.float32).flatten()


_registry: dict[str, EmbeddingModel] = {"sface": SFaceModel()}


def register_embedding_model(model: EmbeddingModel) -> None:
    """Register a custom embedding backend under its own name."""
    _registry[model.name] = model
    logger.info("Registered face embedding model '%s'", model.name)


def select_model(name: str | None = None) -> EmbeddingModel | None:
    """Return the selected backend, or None when it is absent/unavailable.

    Never raises: a missing backend degrades to 'model unavailable'.
    """
    settings = get_settings()
    chosen = name if name is not None else getattr(settings, "face_embedding_model", "sface")
    if chosen == "none":
        return None
    model = _registry.get(chosen)
    if model is None:
        logger.warning("Face embedding model '%s' not registered; abstaining", chosen)
        return None
    if not model.available():
        logger.warning("Face embedding model '%s' not available (weights missing?)", chosen)
        return None
    return model


def cosine_similarity(a: Any, b: Any) -> float:
    """Cosine similarity between two (L2-normalizable) embedding vectors."""
    a = np.asarray(a, dtype=np.float32).flatten()
    b = np.asarray(b, dtype=np.float32).flatten()
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
