"""Shared dataclasses for the tampering module."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Indicator:
    """One forensic signal that fired (or an informational finding)."""

    type: str                 # e.g. "ela_inconsistency"
    severity: str             # info | low | medium | high
    confidence: float         # 0.0-1.0 self-assessed confidence of the signal
    note: str = ""            # human-readable explanation (UI text)
    details: dict[str, Any] = field(default_factory=dict)  # raw evidence

    def to_dict(self) -> dict[str, Any]:
        d = {
            "type": self.type,
            "severity": self.severity,
            "confidence": round(float(self.confidence), 3),
        }
        if self.note:
            d["note"] = self.note
        if self.details:
            d["details"] = self.details
        return d


@dataclass
class RegionFlag:
    """A suspicious region (bounding box) with a reason."""

    x: int
    y: int
    w: int
    h: int
    kind: str        # e.g. "copy_move", "ela_bright", "photo_boundary"
    score: float     # 0..1 strength of the local evidence

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": int(self.x), "y": int(self.y),
            "w": int(self.w), "h": int(self.h),
            "kind": self.kind,
            "score": round(float(self.score), 3),
        }


@dataclass
class ForensicsImage:
    """Loaded image + the exact JPEG used for ELA, kept together so the
    fusion layer can reason about evidence provenance."""

    path: str
    width: int
    height: int
    bgr: Any                     # np.ndarray BGR (untouched decode of the file)
    gray: Any                    # np.ndarray float32 grayscale
    ela_jpeg_path: str | None    # where the ELA reference JPEG was written
    warnings: list[str] = field(default_factory=list)
