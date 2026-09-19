"""Rule definitions shared across rulesets.

A ``Rule`` is the unit of extensibility: a ruleset is just an ordered
list of rules. Adding countries / document types later means writing
new ``Rule`` callables and registering a new ``Ruleset`` — the engine
(see ``engine.py``) never changes.

Conventions:
- A rule receives ``(outcome, fields, ctx)`` and appends
  :class:`ValidationCheck`s to ``outcome.checks``.
- Field values come from ``_field(fields, name)`` so both the flat
  OCR row format and the structured field map work.
- Rules never raise: they downgrade to WARNING with an explanatory
  message when inputs are unusable (advisory tool, not a brittle gate).

IMPORTANT: these rules are GENERIC, simplified checks (ICAO 9303-style
MRZ check digits, ISO dates, common numbering shapes). They do NOT
represent any real country's full document rules.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable
from typing import Any

from app.services.validation.checks import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARNING,
    ValidationOutcome,
)

# A rule mutates the outcome in place.
Rule = Callable[[ValidationOutcome, dict[str, Any], dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Field access helpers (tolerate OCR rows and structured maps)
# ---------------------------------------------------------------------------
def _field(fields: dict[str, Any], name: str) -> tuple[str | None, float]:
    """Return (value, confidence) for a field, or (None, 0.0) if absent."""
    raw = fields.get(name)
    if raw is None:
        return None, 0.0
    if isinstance(raw, dict):
        value = raw.get("value")
        conf = raw.get("confidence", 0.0)
    else:
        value = getattr(raw, "value", raw)
        conf = getattr(raw, "confidence", 0.0)
    if value is None:
        return None, float(conf or 0.0)
    value = str(value).strip()
    return (value or None), float(conf or 0.0)


def _iso_date(value: str | None) -> _dt.date | None:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _check(outcome: ValidationOutcome, field: str, status: str, message: str,
           rule: str | None = None) -> None:
    from app.services.validation.checks import ValidationCheck

    outcome.checks.append(ValidationCheck(field=field, status=status, message=message, rule=rule))


# ---------------------------------------------------------------------------
# Generic rules
# ---------------------------------------------------------------------------
def required_fields(*names: str) -> Rule:
    """All listed fields must be present with a non-empty value."""
    rule_id = f"required_fields[{','.join(names)}]"

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        for name in names:
            value, _conf = _field(fields, name)
            if value:
                _check(outcome, name, STATUS_PASS, f"'{name}' present.", rule_id)
            else:
                _check(outcome, name, STATUS_FAIL, f"Required field '{name}' missing or empty.", rule_id)

    rule.__name__ = rule_id
    return rule


def date_fields(*names: str) -> Rule:
    """Listed date fields must parse as valid ISO dates (YYYY-MM-DD)."""
    rule_id = f"date_format[{','.join(names)}]"

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        for name in names:
            value, _conf = _field(fields, name)
            if value is None:
                _check(outcome, name, STATUS_WARNING, f"'{name}' absent; date format not checkable.", rule_id)
                continue
            if _iso_date(value) is not None:
                _check(outcome, name, STATUS_PASS, f"'{name}' is a valid date ({value}).", rule_id)
            else:
                _check(outcome, name, STATUS_FAIL, f"'{name}' is not a valid date: '{value}'.", rule_id)

    rule.__name__ = rule_id
    return rule


def expiry_validity(*names: str) -> Rule:
    """Document expiry must be in the future (WARNING when already expired)."""
    rule_id = f"expiry_validity[{','.join(names)}]"

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        for name in names:
            value, _conf = _field(fields, name)
            parsed = _iso_date(value)
            if parsed is None:
                _check(outcome, name, STATUS_WARNING, f"Expiry '{value}' unreadable; validity unknown.", rule_id)
                continue
            today = _dt.date.today()
            if parsed >= today:
                _check(outcome, name, STATUS_PASS, f"Document valid until {parsed.isoformat()}.", rule_id)
            else:
                _check(outcome, name, STATUS_WARNING,
                       f"Document expired on {parsed.isoformat()} ({(today - parsed).days} days ago).", rule_id)

    rule.__name__ = rule_id
    return rule


def dob_in_past(name: str = "date_of_birth") -> Rule:
    """Date of birth must be in the past and plausibly old (>= 100y ⇒ warning)."""

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        value, _conf = _field(fields, name)
        parsed = _iso_date(value)
        if parsed is None:
            _check(outcome, name, STATUS_WARNING, f"Birth date '{value}' unreadable; sanity check skipped.", "dob_sanity")
            return
        today = _dt.date.today()
        if parsed > today:
            _check(outcome, name, STATUS_FAIL, f"Birth date {parsed.isoformat()} is in the future.", "dob_sanity")
        elif (today - parsed).days > 100 * 365:
            _check(outcome, name, STATUS_WARNING, f"Birth date {parsed.isoformat()} implies age > 100.", "dob_sanity")
        else:
            _check(outcome, name, STATUS_PASS, f"Birth date {parsed.isoformat()} is plausible.", "dob_sanity")

    rule.__name__ = f"dob_in_past[{name}]"
    return rule


def regex_format(name: str, pattern: str, human: str, *,
                 severity_fail: str = STATUS_FAIL) -> Rule:
    """Value must fully match ``pattern`` (compiled once, case-sensitive)."""
    rx = re.compile(pattern)
    rule_id = f"format[{name}]"

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        value, _conf = _field(fields, name)
        if value is None:
            _check(outcome, name, STATUS_WARNING, f"'{name}' absent; format not checkable.", rule_id)
            return
        if rx.fullmatch(value):
            _check(outcome, name, STATUS_PASS, f"'{name}' matches expected format ({human}).", rule_id)
        else:
            _check(outcome, name, severity_fail,
                   f"'{name}' value '{value}' does not match expected format ({human}).", rule_id)

    rule.__name__ = rule_id
    return rule


def check_digit_field(name: str, label: str | None = None) -> Rule:
    """Surface the OCR-stage MRZ check-digit verdict for one field."""
    rule_id = f"mrz_check_digit[{name}]"

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        raw = fields.get(name)
        ok = raw.get("check_digit_ok") if isinstance(raw, dict) else getattr(raw, "check_digit_ok", None)
        value, _conf = _field(fields, name)
        if value is None:
            _check(outcome, name, STATUS_WARNING, f"'{name}' absent; check digit not verifiable.", rule_id)
        elif ok is True:
            _check(outcome, name, STATUS_PASS, f"{label or name}: MRZ check digit verified.", rule_id)
        elif ok is False:
            _check(outcome, name, STATUS_FAIL,
                   f"{label or name}: MRZ check digit MISMATCH — possible OCR error or forgery; verify manually.", rule_id)
        else:
            _check(outcome, name, STATUS_WARNING, f"{label or name}: no MRZ check digit available.", rule_id)

    rule.__name__ = rule_id
    return rule


def mrz_cross_consistency(pairs: list[tuple[str, str]]) -> Rule:
    """Extracted fields must agree with the independently parsed MRZ.

    ``pairs`` maps extracted-field name → MRZ element label (e.g.
    ``("date_of_birth", "date_of_birth")``). The MRZ values come from
    ``ctx['mrz_fields']`` — the structured parse of the raw MRZ block,
    produced separately from the field extraction. When both sides are
    readable they must match; mismatches are strong tampering/OCR signals.
    """
    rule_id = "mrz_consistency"

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:
        mrz_fields = ctx.get("mrz_fields") or {}
        if not mrz_fields:
            _check(outcome, "mrz", STATUS_WARNING,
                   "No MRZ on document (or unreadable) — cross-consistency not verifiable.", rule_id)
            return
        for field_name, mrz_name in pairs:
            extracted, _c = _field(fields, field_name)
            mrz_value, _c2 = _field(mrz_fields, mrz_name)
            if mrz_value is None:
                continue  # MRZ element absent (e.g. no personal number)
            if extracted is None:
                _check(outcome, field_name, STATUS_WARNING,
                       f"'{field_name}' extracted value missing although MRZ has '{mrz_value}'.", rule_id)
            elif _norm(extracted) == _norm(mrz_value):
                _check(outcome, field_name, STATUS_PASS,
                       f"'{field_name}' agrees with the independently parsed MRZ.", rule_id)
            else:
                _check(outcome, field_name, STATUS_FAIL,
                       f"Extracted '{field_name}' ('{extracted}') != MRZ value ('{mrz_value}') — "
                       "verify against the document image.", rule_id)

    rule.__name__ = rule_id
    return rule


def mrz_composite_digest() -> Rule:
    """TD3 composite check digit over MRZ line 2 (whole-line integrity)."""

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        mrz_raw = ctx.get("mrz_raw") or ""
        lines = [ln for ln in mrz_raw.splitlines() if ln.strip()]
        if not lines:
            _check(outcome, "mrz", STATUS_WARNING,
                   "No MRZ raw text available; composite integrity not checkable.", "mrz_composite")
            return
        from app.services.ocr.mrz import parse_mrz

        parsed = parse_mrz(lines)
        if parsed is None:
            _check(outcome, "mrz", STATUS_WARNING,
                   "MRZ raw text could not be re-parsed; composite integrity unknown.", "mrz_composite")
        elif parsed.composite_digest_ok is True:
            _check(outcome, "mrz", STATUS_PASS,
                   "MRZ composite check digit verified (line integrity intact).", "mrz_composite")
        elif parsed.composite_digest_ok is False:
            _check(outcome, "mrz", STATUS_FAIL,
                   "MRZ composite check digit MISMATCH — MRZ data corrupted or altered.", "mrz_composite")
        else:
            _check(outcome, "mrz", STATUS_WARNING,
                   f"MRZ format {parsed.format} has no composite check digit; skipped.", "mrz_composite")

    rule.__name__ = "mrz_composite_digest"
    return rule


_MRZ_DOC_CODES = {"P": "passport", "V": "visa"}


def mrz_doc_code_consistency() -> Rule:
    """MRZ document code must match the document type being validated."""

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:
        mrz_fields = ctx.get("mrz_fields") or {}
        doc_type = str(ctx.get("doc_type") or "unknown").lower()
        code, _c = _field(mrz_fields, "document_code")
        if not code:
            _check(outcome, "mrz", STATUS_WARNING,
                   "MRZ document code unreadable; type consistency not checkable.", "mrz_doc_code")
            return
        expected = _MRZ_DOC_CODES.get(code.strip()[:1].upper())
        if expected is None:
            _check(outcome, "mrz", STATUS_WARNING,
                   f"MRZ document code '{code}' is not P/V/I — unknown document class.", "mrz_doc_code")
        elif expected == doc_type:
            _check(outcome, "mrz", STATUS_PASS,
                   f"MRZ document code '{code}' matches document type '{doc_type}'.", "mrz_doc_code")
        else:
            _check(outcome, "mrz", STATUS_FAIL,
                   f"MRZ document code '{code}' implies a {expected}, but this run validates a "
                   f"{doc_type} — mismatch must be reviewed.", "mrz_doc_code")

    rule.__name__ = "mrz_doc_code_consistency"
    return rule


def _norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def mrz_presence(expected: bool = True) -> Rule:
    """Document type should (or should not) carry a machine-readable zone."""

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:
        has_mrz = bool(ctx.get("mrz_raw"))
        if has_mrz is expected:
            _check(outcome, "mrz", STATUS_PASS,
                   "MRZ present and parsed." if expected else "No MRZ (as expected for this type).", "mrz_presence")
        elif has_mrz:
            _check(outcome, "mrz", STATUS_PASS, "MRZ present and parsed.", "mrz_presence")
        else:
            _check(outcome, "mrz", STATUS_WARNING,
                   "Expected an MRZ on this document type but none was found/readable.", "mrz_presence")

    rule.__name__ = f"mrz_presence[{expected}]"
    return rule


# ---------------------------------------------------------------------------
# Visa-specific rules
# ---------------------------------------------------------------------------
VISA_ENTRY_VALUES = {"MULT", "MULTIPLE", "SINGLE", "M", "S"}
VISA_STAY_PATTERN = re.compile(r"^(\d{1,3})\s*(DAYS?|MONTHS?)$", re.IGNORECASE)


def visa_entry_validity(name: str = "entry_validity") -> Rule:
    """Entry marker must be one of the known values (MULT/MULTIPLE/SINGLE/M/S)."""
    rule_id = f"visa_entries[{name}]"

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        value, _conf = _field(fields, name)
        if value is None:
            _check(outcome, name, STATUS_WARNING, "Entry validity absent; enter manually.", rule_id)
            return
        if _norm(value) in {_norm(v) for v in VISA_ENTRY_VALUES}:
            _check(outcome, name, STATUS_PASS, f"Entry validity '{value}' is a known value.", rule_id)
        else:
            _check(outcome, name, STATUS_FAIL,
                   f"Entry validity '{value}' is not a recognized value {sorted(VISA_ENTRY_VALUES)}.", rule_id)

    rule.__name__ = rule_id
    return rule


def visa_stay_duration(name: str = "stay_duration", max_days: int = 365) -> Rule:
    """Stay duration must parse as '<n> DAYS|MONTHS' and be within range."""
    rule_id = f"visa_stay[{name}]"

    def rule(outcome: ValidationOutcome, fields: dict[str, Any], ctx: dict[str, Any]) -> None:  # noqa: ARG001
        value, _conf = _field(fields, name)
        if value is None:
            _check(outcome, name, STATUS_WARNING, "Stay duration absent; enter manually.", rule_id)
            return
        m = VISA_STAY_PATTERN.match(value.replace("_", " ").strip())
        if not m:
            _check(outcome, name, STATUS_FAIL,
                   f"Stay duration '{value}' does not parse as '<n> DAYS/MONTHS'.", rule_id)
            return
        n = int(m.group(1))
        unit = m.group(2).upper()
        days = n * 30 if unit.startswith("MONTH") else n
        if days <= 0:
            _check(outcome, name, STATUS_FAIL, f"Stay duration '{value}' is zero/negative.", rule_id)
        elif days > max_days:
            _check(outcome, name, STATUS_WARNING,
                   f"Stay duration '{value}' exceeds {max_days} days — unusual for a visa; verify.", rule_id)
        else:
            _check(outcome, name, STATUS_PASS, f"Stay duration '{value}' is plausible.", rule_id)

    rule.__name__ = rule_id
    return rule
