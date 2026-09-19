"""AI service interfaces + placeholder providers — THE PLUGGABLE SEAM.

Every AI capability (preprocessing, OCR, validation, tampering, face,
risk) is defined as a small Protocol. Pipeline stages call providers
through these interfaces only; they never import engine libraries.

How to plug in a real engine later (e.g. Step 3 OCR):
    1. Create ``app/services/ocr_paddle.py`` implementing OcrProvider.
    2. Register it: ``register_ocr_provider("paddleocr", PaddleOcrProvider())``.
    3. Set ``APP_AI_OCR_PROVIDER=paddleocr`` in the environment.
    4. No pipeline/API/test changes required.

Until a capability is implemented, its placeholder reports honest
``implemented: False`` payloads — never fake results.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider interfaces (Protocols)
# ---------------------------------------------------------------------------
class PreprocessingProvider(Protocol):
    name: str

    def preprocess(self, image_path: str) -> dict[str, Any]:
        """Return preprocessed_image_path + quality metrics."""
        ...


class OcrProvider(Protocol):
    name: str

    def extract(self, image_path: str, doc_type_hint: str | None = None) -> dict[str, Any]:
        """Return fields[{name,value,confidence}], mrz_raw, doc_type_detected."""
        ...


class ValidationProvider(Protocol):
    name: str

    def validate(
        self,
        ocr_fields: list[dict],
        doc_type_hint: str,
        mrz_raw: str | None = None,
    ) -> dict[str, Any]:
        """Return checks[], failures[], registry lookup result (MOCK)."""
        ...


class TamperingProvider(Protocol):
    name: str

    def analyze(self, image_path: str) -> dict[str, Any]:
        """Return signals[], score(0..1), confidence, explanation."""
        ...


class FaceVerificationProvider(Protocol):
    name: str

    def verify(self, document_image_path: str, probe_image_path: str | None) -> dict[str, Any]:
        """Return similarity, threshold, liveness_cues, confidence."""
        ...


class RiskScoringProvider(Protocol):
    name: str

    def score(self, stage_outputs: dict[str, Any]) -> dict[str, Any]:
        """Return score(0..1|None), band, contributions[], routing."""
        ...


# ---------------------------------------------------------------------------
# Placeholder providers (honest: implemented=False, never fake results)
# ---------------------------------------------------------------------------
class _PlaceholderBase:
    """Base for placeholder providers; subclasses set ``capability``."""

    capability: str = ""

    @property
    def name(self) -> str:
        return f"placeholder-{self.capability}"


class PlaceholderPreprocessing(_PlaceholderBase):
    """Pass-through: copies the original path forward without processing."""

    capability = "preprocessing"

    def preprocess(self, image_path: str) -> dict[str, Any]:
        return {
            "implemented": False,
            "engine": self.name,
            "note": "No preprocessing engine configured yet (Step 2+). Image passed through unchanged.",
            "preprocessed_image_path": image_path,
            "quality_metrics": None,
        }


class PlaceholderOcr(_PlaceholderBase):
    capability = "ocr"

    def extract(self, image_path: str, doc_type_hint: str | None = None) -> dict[str, Any]:  # noqa: ARG002
        return {
            "implemented": False,
            "engine": self.name,
            "note": "No OCR engine configured yet (Step 3). No fields extracted.",
            "ocr_fields": [],
            "fields": [],  # deprecated alias for ocr_fields
            "mrz_raw": None,
            "doc_type_detected": None,
        }


class PlaceholderValidation(_PlaceholderBase):
    capability = "validation"

    def validate(
        self,
        ocr_fields: list[dict],
        doc_type_hint: str,
        mrz_raw: str | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        return {
            "implemented": False,
            "engine": self.name,
            "note": "No validation engine configured yet (Step 5). Nothing validated.",
            "checks": [],
            "failures": [],
            "registry": None,
            "doc_type_hint": doc_type_hint,
            "validation": {"checks": [], "failures": [], "registry": None},
        }


class PlaceholderTampering(_PlaceholderBase):
    capability = "tampering"

    def analyze(self, image_path: str) -> dict[str, Any]:  # noqa: ARG002
        return {
            "implemented": False,
            "engine": self.name,
            "note": "No tampering-detection engine configured yet (Step 5).",
            "signals": [],
            "score": None,
            "confidence": None,
            "explanation": "Tampering analysis unavailable; human inspection required.",
            "tampering": {"signals": [], "score": None, "confidence": None},
        }


class PlaceholderFace(_PlaceholderBase):
    capability = "face"

    def verify(self, document_image_path: str, probe_image_path: str | None) -> dict[str, Any]:  # noqa: ARG002
        return {
            "implemented": False,
            "engine": self.name,
            "note": "No face-verification engine configured yet (Step 6).",
            "similarity": None,
            "threshold": None,
            "liveness_cues": None,
            "confidence": None,
            "explanation": "Face verification unavailable; human inspection required.",
            "face_verification": {"similarity": None, "threshold": None},
        }


class PlaceholderRisk(_PlaceholderBase):
    capability = "risk"

    def score(self, stage_outputs: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return {
            "implemented": False,
            "engine": self.name,
            "note": "No risk engine configured yet (Step 7). No score produced.",
            "score": None,
            "band": None,
            "contributions": [],
            "human_review_required": True,
            "risk": {"score": None, "band": None, "human_review_required": True},
        }


# ---------------------------------------------------------------------------
# Provider registry (capability -> {name -> instance})
# ---------------------------------------------------------------------------
_registry: dict[str, dict[str, Any]] = {
    "preprocessing": {},
    "ocr": {},
    "validation": {},
    "tampering": {},
    "face": {},
    "risk": {},
}

_placeholders: dict[str, Any] = {
    "preprocessing": PlaceholderPreprocessing(),
    "ocr": PlaceholderOcr(),
    "validation": PlaceholderValidation(),
    "tampering": PlaceholderTampering(),
    "face": PlaceholderFace(),
    "risk": PlaceholderRisk(),
}

_setting_keys: dict[str, str] = {
    "preprocessing": "ai_preprocessing_provider",
    "ocr": "ai_ocr_provider",
    "validation": "ai_validation_provider",
    "tampering": "ai_tampering_provider",
    "face": "ai_face_provider",
    "risk": "ai_risk_provider",
}

_public_types: dict[str, str] = {
    "preprocessing": "PreprocessingProvider",
    "ocr": "OcrProvider",
    "validation": "ValidationProvider",
    "tampering": "TamperingProvider",
    "face": "FaceVerificationProvider",
    "risk": "RiskScoringProvider",
}


def register_provider(capability: str, name: str, provider: Any) -> None:
    """Register a real provider implementation for a capability."""
    if capability not in _registry:
        raise ValueError(f"Unknown capability '{capability}'")
    _registry[capability][name] = provider
    logger.info("Registered %s provider '%s'", capability, name)


# Convenience one-liners for real engines:
def register_preprocessing_provider(name: str, provider: PreprocessingProvider) -> None:
    register_provider("preprocessing", name, provider)


def register_ocr_provider(name: str, provider: OcrProvider) -> None:
    register_provider("ocr", name, provider)


def register_validation_provider(name: str, provider: ValidationProvider) -> None:
    register_provider("validation", name, provider)


def register_tampering_provider(name: str, provider: TamperingProvider) -> None:
    register_provider("tampering", name, provider)


def register_face_provider(name: str, provider: FaceVerificationProvider) -> None:
    register_provider("face", name, provider)


def register_risk_provider(name: str, provider: RiskScoringProvider) -> None:
    register_provider("risk", name, provider)


def _register_step4_engines() -> None:
    """Register the Step-4 preprocessing + OCR providers (idempotent)."""
    if _registry["preprocessing"] or _registry["ocr"]:
        return  # already registered
    try:
        from app.services.ocr.provider import OcrPreprocessingProvider, RapidOcrProvider

        register_preprocessing_provider("cv2-preprocess", OcrPreprocessingProvider())
        register_ocr_provider("rapidocr", RapidOcrProvider())
    except Exception as exc:  # noqa: BLE001 - missing deps must not kill startup
        logger.warning("Step-4 OCR providers not registered: %s", exc)
    try:
        from app.services.ocr.engine_tesseract import TesseractOcrProvider

        register_ocr_provider("tesseract", TesseractOcrProvider())
    except Exception:  # noqa: BLE001
        pass  # optional


def _register_step5_engines() -> None:
    """Register the Step-5 validation provider (idempotent)."""
    if _registry["validation"]:
        return  # already registered
    try:
        from app.services.validation.provider import RuleEngineValidationProvider

        register_validation_provider("rule-engine", RuleEngineValidationProvider())
    except Exception as exc:  # noqa: BLE001 - missing deps must not kill startup
        logger.warning("Step-5 validation provider not registered: %s", exc)


def _register_step6_engines() -> None:
    """Register the Step-6 tampering provider (idempotent)."""
    if _registry["tampering"]:
        return  # already registered
    try:
        from app.services.tampering.provider import ForensicTamperingProvider

        register_tampering_provider("forensic-heuristics", ForensicTamperingProvider())
    except Exception as exc:  # noqa: BLE001 - missing deps must not kill startup
        logger.warning("Step-6 tampering provider not registered: %s", exc)


def _register_step8_engines() -> None:
    """Register the Step-8 risk-assessment provider (idempotent)."""
    if _registry["risk"]:
        return  # already registered
    try:
        from app.services.risk.provider import WeightedRulesRiskProvider

        register_risk_provider("weighted-rules-v2", WeightedRulesRiskProvider())
    except Exception as exc:  # noqa: BLE001 - missing deps must not kill startup
        logger.warning("Step-8 risk provider not registered: %s", exc)


def _register_step7_engines() -> None:
    """Register the Step-7 face-verification provider (idempotent).

    Registration does NOT load model weights — the engine loads YuNet/SFace
    lazily. When the weights are missing the provider still runs and reports
    an honest INCONCLUSIVE ('model unavailable') payload.
    """
    if _registry["face"]:
        return  # already registered
    try:
        from app.services.face.provider import CvFaceVerificationProvider

        register_face_provider("cv-face-yunet-sface", CvFaceVerificationProvider())
    except Exception as exc:  # noqa: BLE001 - missing deps must not kill startup
        logger.warning("Step-7 face provider not registered: %s", exc)


def get_provider(capability: str) -> Any:
    """Return the configured provider, falling back to the placeholder."""
    if capability not in _registry:
        raise ValueError(f"Unknown capability '{capability}'")
    settings = get_settings()
    selected = getattr(settings, _setting_keys[capability])
    if capability in {"preprocessing", "ocr"}:
        _register_step4_engines()
    if capability == "validation":
        _register_step5_engines()
    if capability == "tampering":
        _register_step6_engines()
    if capability == "face":
        _register_step7_engines()
    if capability == "risk":
        _register_step8_engines()
    provider = _registry[capability].get(selected)
    if provider is None:
        if selected != "placeholder":
            logger.warning(
                "Provider '%s' for '%s' not registered; falling back to placeholder",
                selected,
                capability,
            )
        return _placeholders[capability]
    return provider


def providers_status() -> dict[str, dict[str, Any]]:
    """Report which provider is active per capability (for /pipeline/info)."""
    settings = get_settings()
    _register_step4_engines()
    _register_step5_engines()
    _register_step6_engines()
    _register_step7_engines()
    _register_step8_engines()
    status: dict[str, dict[str, Any]] = {}
    for capability in _registry:
        selected = getattr(settings, _setting_keys[capability])
        registered = sorted(_registry[capability].keys())
        active = get_provider(capability)
        status[capability] = {
            "configured": selected,
            "registered": registered,
            "active": active.name,
            "is_placeholder": capability == "preprocessing"
            and isinstance(active, PlaceholderPreprocessing)
            or active.name.startswith("placeholder-"),
            "interface": _public_types[capability],
        }
    return status
