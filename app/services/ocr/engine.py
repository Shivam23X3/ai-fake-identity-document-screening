"""OCR engine abstraction: RapidOCR (default) + optional Tesseract.

Both engines return the same :class:`OcrOutput`: full text plus word-level
boxes with per-word confidence. Field extraction (``fields.py``) consumes
that, never a specific engine — swap engines without touching extraction.

RapidOCR needs no system dependencies (ONNX runtime + bundled models) and
is therefore the default. Tesseract gives better results on some scans if
the binary is installed; the module auto-selects whichever is available.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

try:  # engines are optional at import time; selection happens lazily
    import cv2
except ImportError as _exc:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    logger.warning("opencv not importable, OCR engines disabled: %s", _exc)


class OcrEngineError(RuntimeError):
    """Raised when no engine can process an image."""


@dataclass
class OcrWord:
    """One recognized token with geometry + confidence."""

    text: str
    confidence: float          # 0..1
    box: list[list[int]]       # 4 corner points [[x,y], ...]
    line: int                  # line index assigned by clustering

    @property
    def left(self) -> int:
        return min(p[0] for p in self.box)

    @property
    def top(self) -> int:
        return min(p[1] for p in self.box)

    @property
    def right(self) -> int:
        return max(p[0] for p in self.box)

    @property
    def bottom(self) -> int:
        return max(p[1] for p in self.box)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)


@dataclass
class OcrOutput:
    """Engine-independent OCR result."""

    engine: str
    words: list[OcrWord] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)   # cleaned text lines
    raw_text: str = ""

    @property
    def mean_confidence(self) -> float | None:
        if not self.words:
            return None
        return sum(w.confidence for w in self.words) / len(self.words)

    def words_in_line(self, index: int) -> list[OcrWord]:
        return [w for w in self.words if w.line == index]


# ---------------------------------------------------------------------------
# RapidOCR engine (default — pip-only, no system binaries)
# ---------------------------------------------------------------------------
class RapidOcrEngine:
    """ONNX-Runtime OCR (PaddleOCR models via rapidocr-onnxruntime)."""

    name = "rapidocr"

    def __init__(self) -> None:
        self._reader: Any = None

    def _lazy_init(self) -> Any:
        if self._reader is None:
            from rapidocr_onnxruntime import RapidOCR  # imported lazily

            self._reader = RapidOCR()
        return self._reader

    def available(self) -> bool:
        try:
            self._lazy_init()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("RapidOCR unavailable: %s", exc)
            return False

    def run(self, image: np.ndarray) -> OcrOutput:
        reader = self._lazy_init()
        result, _elapsed = reader(image)
        words: list[OcrWord] = []
        if result:
            for box, text, conf in result:
                text = (text or "").strip()
                if not text:
                    continue
                words.append(
                    OcrWord(
                        text=text,
                        confidence=float(conf),
                        box=[[int(x), int(y)] for x, y in box],
                        line=0,  # assigned by _assign_lines()
                    )
                )
        output = OcrOutput(engine=self.name, words=words)
        _assign_lines(output)
        return output


# ---------------------------------------------------------------------------
# Tesseract engine (optional — requires the tesseract binary on PATH)
# ---------------------------------------------------------------------------
class TesseractOcrEngine:
    """pytesseract wrapper with TSV word confidences."""

    name = "tesseract"

    def __init__(self, lang: str = "eng", config: str = "--psm 6") -> None:
        self.lang = lang
        self.config = config

    @staticmethod
    def available() -> bool:
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
            return True
        except Exception:  # noqa: BLE001 - missing binary or package
            return False

    def run(self, image: np.ndarray) -> OcrOutput:
        import pytesseract

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        tsv = pytesseract.image_to_data(
            gray, lang=self.lang, config=self.config,
            output_type=pytesseract.Output.DICT,
        )
        words: list[OcrWord] = []
        n = len(tsv["text"])
        for i in range(n):
            text = (tsv["text"][i] or "").strip()
            conf_raw = float(tsv["conf"][i])
            if not text or conf_raw < 0:
                continue
            x, y, w, h = tsv["left"][i], tsv["top"][i], tsv["width"][i], tsv["height"][i]
            words.append(
                OcrWord(
                    text=text,
                    confidence=conf_raw / 100.0,
                    box=[[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
                    line=int(tsv["block_num"][i]),  # re-clustered below
                )
            )
        output = OcrOutput(engine=self.name, words=words)
        _assign_lines(output)
        return output


# ---------------------------------------------------------------------------
# Line clustering + text assembly (shared)
# ---------------------------------------------------------------------------
def _assign_lines(output: OcrOutput) -> None:
    """Group words into text lines by vertical overlap of their boxes."""
    if not output.words:
        return
    ordered = sorted(output.words, key=lambda w: (w.top, w.left))
    line_idx = 0
    current_top: int | None = None
    current_bottom: int | None = None
    for word in ordered:
        if current_top is None:
            current_top, current_bottom = word.top, word.bottom
            word.line = line_idx
            continue
        overlap = min(current_bottom, word.bottom) - max(current_top, word.top)
        # Same line when the box vertically overlaps the current band enough.
        if overlap > 0.45 * min(current_bottom - current_top, word.height):
            word.line = line_idx
            current_bottom = max(current_bottom, word.bottom)
        else:
            line_idx += 1
            word.line = line_idx
            current_top, current_bottom = word.top, word.bottom

    output.lines = []
    for idx in range(line_idx + 1):
        row = sorted(output.words_in_line(idx), key=lambda w: w.left)
        if row:
            output.lines.append(" ".join(w.text for w in row))
    output.raw_text = "\n".join(output.lines)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
_ENGINE_CLASSES: dict[str, type] = {
    "rapidocr": RapidOcrEngine,
    "tesseract": TesseractOcrEngine,
}


def get_engine(preferred: str = "rapidocr") -> Any:
    """Return an engine instance for ``preferred``, falling back sensibly.

    ``preferred``: ``rapidocr`` | ``tesseract`` | ``auto``.
    Raises :class:`OcrEngineError` when nothing is usable.
    """
    order = [preferred] if preferred != "auto" else ["rapidocr", "tesseract"]
    for name in order:
        cls = _ENGINE_CLASSES.get(name)
        if cls is None:
            logger.warning("Unknown OCR engine '%s', trying fallbacks", name)
            continue
        engine = cls()
        if engine.available():
            return engine
    raise OcrEngineError(
        "No OCR engine available. Install 'rapidocr-onnxruntime' (pip) "
        "and/or the Tesseract binary."
    )
