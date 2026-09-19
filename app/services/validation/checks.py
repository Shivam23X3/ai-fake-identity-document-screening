"""Validation check primitives: one check result + the overall outcome."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Statuses per the Step-5 contract.
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_WARNING = "WARNING"


@dataclass
class ValidationCheck:
    """Result of a single validation rule."""

    field: str
    status: str  # PASS | FAIL | WARNING
    message: str
    rule: str | None = None  # rule id that produced this check

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"field": self.field, "status": self.status, "message": self.message}
        if self.rule:
            out["rule"] = self.rule
        return out


@dataclass
class ValidationOutcome:
    """Aggregate result of running a ruleset over extracted fields."""

    is_valid: bool
    checks: list[ValidationCheck] = field(default_factory=list)
    confidence: float | None = None
    doc_type: str = "unknown"
    ruleset: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        return [c.message for c in self.checks if c.status == STATUS_FAIL]

    @property
    def warnings(self) -> list[str]:
        return [c.message for c in self.checks if c.status == STATUS_WARNING]

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "checks": [c.to_dict() for c in self.checks],
            "confidence": round(self.confidence, 3) if self.confidence is not None else None,
            "doc_type": self.doc_type,
            "ruleset": self.ruleset,
            "failures": self.failures,
            "warnings": self.warnings,
            "notes": list(self.notes),
        }
