"""Trained tampering-detection model interface.

The forensic pipeline never depends on a trained classifier being present.
When one becomes available it plugs in here without touching the detectors,
the fusion layer, or any pipeline code:

    from app.services.tampering.model import register_tampering_model

    class MyCnnModel:
        name = "my-cnn-v1"
        def predict(self, bgr_u8, gray_u8):
            # return {"probability": 0.0..1.0, "map": <optional np.ndarray>,
            #         "label": "tampered"|"authentic", "version": "1.0"}
            ...

    register_tampering_model(MyCnnModel())

Set ``APP_AI_TAMPERING_MODEL=my-cnn-v1`` (or ``none``) to select. The
selected model is loaded lazily and failures are logged, never raised.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class TamperingModel(Protocol):
    """Contract for a trained manipulation detector (CNN or otherwise)."""

    name: str

    def predict(self, bgr_u8: Any, gray_u8: Any) -> dict[str, Any]:
        """Return {probability: 0..1, label?: str, map?: np.ndarray, version?: str}."""
        ...


class NoModel:
    """Default: no trained model available — the pipeline must abstain honestly."""

    name = "none"

    @staticmethod
    def available() -> bool:
        return False

    def predict(self, bgr_u8: Any, gray_u8: Any) -> dict[str, Any]:  # noqa: ARG002
        return {"probability": None, "note": "No trained tampering model registered."}


_model_registry: dict[str, TamperingModel] = {}
_model_name: str | None = None


def register_tampering_model(model: TamperingModel) -> None:
    """Register a trained model instance under its own name."""
    _model_registry[model.name] = model
    logger.info("Registered tampering model '%s'", model.name)


def select_model(name: str | None = None) -> TamperingModel:
    """Return the selected model, or :class:`NoModel` (never raises)."""
    global _model_name
    settings = get_settings()
    chosen = name if name is not None else getattr(settings, "ai_tampering_model", "none")
    _model_name = chosen
    if chosen == "none":
        return NoModel()
    model = _model_registry.get(chosen)
    if model is None:
        logger.warning("Tampering model '%s' not registered; abstaining", chosen)
        return NoModel()
    return model


def active_model_name() -> str:
    return select_model().name
