"""Risk-assessment engine — deterministic, explainable weighted rules (Step 8).

    OCR + validation + tampering + face + expiry + missing fields
    + suspicious patterns + metadata anomalies + stage outcomes
                        │
                        ▼
        fixed weights → fused score (0–100) → level + reasons
                        │
     reasons[] / contributions[] explain every point

Explainability is the design constraint: the score is a plain weighted sum
of documented constants (constants.py WEIGHTS), so any reviewer can
reconstruct exactly why a run landed in its level. Uncertainty is never
silently ignored — degraded/missing engines add their own points and force
human review, because absence of evidence is not evidence of authenticity.

Honesty policy: the output is advisory only. No automatic accept/reject
exists anywhere in this module; routing can only ever *require* more human
attention, never less.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from typing import Any

from app.core.config import get_settings
from app.services.risk.constants import (
    CHAR_CONFUSION_PAIRS,
    CONF_BAND_EDGE_PENALTY,
    CONF_BASE,
    CONF_DEGRADED_CAP,
    CONF_ERROR_FLOOR,
    CONF_PER_DEGRADED_STAGE,
    FACE_CONF_HIGH,
    LEVELS,
    LEVEL_CRITICAL,
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    NUMBER_FIELD_HINTS,
    OCR_FIELD_CAP,
    OCR_LOW_CONFIDENCE_THRESHOLD,
    REGISTRY_BAD_STATUSES,
    REVIEW_SCORE_MIN,
    WEIGHTS,
)

logger = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Common OCR digit/letter confusions for date fields (best-effort repair).
_DATE_DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8"})

# Letters that look like digits AND are rare inside real document numbers.
# ('l' and 'u' are deliberately excluded — they occur legitimately and would
# make this detector cry wolf.) See CHAR_CONFUSION_PAIRS for the full map.
_SUSPECT_CHARS_IN_NUMBERS = frozenset("oiqszbg")


class RiskAssessmentError(RuntimeError):
    """Raised when stage outputs are so malformed that fusion cannot run."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _clamp100(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, f))


def _verdict_of(block: Any, *keys: str) -> str | None:
    """Pull a verdict-ish string out of a nested stage block."""
    if not isinstance(block, dict):
        return None
    for k in keys:
        v = block.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _field_rows(ocr: Any) -> list[dict[str, Any]]:
    """Normalize every supported OCR payload shape into field rows.

    The pipeline context publishes ``ocr_fields`` as the flat row list
    (see OcrStage.provides); direct engine callers may pass the whole OCR
    payload dict or a {name: {value, confidence}} map. Accept all three.
    """
    rows: list[dict[str, Any]] = []
    raw: Any = ocr
    if isinstance(ocr, dict):
        raw = ocr.get("ocr_fields")
        if raw is None:
            raw = ocr.get("fields")
    if isinstance(raw, dict):
        raw = [
            {"name": name, **(val if isinstance(val, dict) else {"value": val})}
            for name, val in raw.items()
        ]
    if not isinstance(raw, list):
        return rows
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            rows.append(item)
    return rows


def _value_of(row: dict[str, Any]) -> str:
    v = row.get("value")
    return str(v).strip() if v not in (None, "") else ""


def _confidence_of(row: dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(row.get("confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# The contribution ledger
# ---------------------------------------------------------------------------
class _Ledger:
    """Accumulates points + the human-readable reasons explaining them."""

    def __init__(self) -> None:
        self.points: float = 0.0
        self.reasons: list[str] = []
        self.contributions: list[dict[str, Any]] = []

    def add(
        self,
        weight_key: str,
        reason: str,
        *,
        signal: str | None = None,
        points: float | None = None,
        details: str = "",
    ) -> float:
        pts = float(WEIGHTS[weight_key]) if points is None else float(points)
        if pts <= 0:
            return self.points
        self.points += pts
        self.reasons.append(reason)
        self.contributions.append(
            {
                "signal": signal or reason,
                "points": round(pts, 1),
                "details": details,
            }
        )
        return self.points

    def capped_add(
        self,
        weight_key: str,
        per_item_points: float,
        n: int,
        reason_fn,
        *,
        signal: str,
        cap: float,
    ) -> float:
        """Add ``n * per_item_points`` clamped to ``cap``; one reason line."""
        n = max(0, int(n))
        if n <= 0:
            return self.points
        pts = min(cap, n * per_item_points)
        self.points += pts
        self.reasons.append(reason_fn(n))
        self.contributions.append(
            {"signal": signal, "points": round(pts, 1), "details": f"{n} item(s), capped at {cap:.0f}"}
        )
        return self.points


# ---------------------------------------------------------------------------
# Signal extractors (one per Step-8 category)
# ---------------------------------------------------------------------------
def _ocr_signal(ocr: Any, ledger: _Ledger) -> None:
    rows = _field_rows(ocr)
    if not rows:
        ledger.add(
            "ocr_unavailable",
            "No usable OCR fields extracted — identity data could not be read",
            signal="OCR unavailable",
        )
        return
    low = [r for r in rows if _confidence_of(r) < OCR_LOW_CONFIDENCE_THRESHOLD]
    ledger.capped_add(
        "ocr_low_confidence_field",
        WEIGHTS["ocr_low_confidence_field"],
        len(low),
        lambda n: f"{n} OCR field(s) below {OCR_LOW_CONFIDENCE_THRESHOLD:.0%} confidence",
        signal="OCR low-confidence fields",
        cap=OCR_FIELD_CAP * WEIGHTS["ocr_low_confidence_field"],
    )
    flagged = [r for r in rows if r.get("flagged")]
    ledger.capped_add(
        "ocr_flagged_field",
        WEIGHTS["ocr_flagged_field"],
        len(flagged),
        lambda n: f"{n} OCR field(s) carry extraction warnings (MRZ/check-digit notes)",
        signal="OCR flagged fields",
        cap=OCR_FIELD_CAP * WEIGHTS["ocr_flagged_field"],
    )


def _validation_signal(validation: Any, ledger: _Ledger) -> None:
    if not isinstance(validation, dict) or validation.get("implemented") is False:
        ledger.add(
            "validation_unavailable",
            "Document validation engine did not run — rule checks unknown",
            signal="Validation unavailable",
        )
        return
    failures = validation.get("failures") or []
    warnings = validation.get("warnings") or []
    if not isinstance(failures, list):
        failures = []
    if not isinstance(warnings, list):
        warnings = []
    if failures:
        first = WEIGHTS["validation_failure_first"]
        extra = WEIGHTS["validation_failure_extra"] * max(0, len(failures) - 1)
        pts = min(WEIGHTS["validation_failures_cap"], first + extra)
        sample = "; ".join(str(f) for f in failures[:2])
        more = f" (+{len(failures) - 2} more)" if len(failures) > 2 else ""
        ledger.add(
            "validation_failure_first",  # key unused when points given
            f"Document validation failed: {sample}{more}",
            signal="Validation failures",
            points=pts,
        )
    if warnings:
        ledger.capped_add(
            "validation_warning_each",
            WEIGHTS["validation_warning_each"],
            len(warnings),
            lambda n: f"{n} document validation warning(s)",
            signal="Validation warnings",
            cap=WEIGHTS["validation_warnings_cap"],
        )
    registry = validation.get("registry")
    if isinstance(registry, dict) and registry.get("status") in REGISTRY_BAD_STATUSES:
        ledger.add(
            "validation_failure_first",
            f"Registry (MOCK demo data) lists document as {registry.get('status')}",
            signal="Registry adverse status",
            points=WEIGHTS["validation_failures_cap"],
        )


def _tampering_signal(tampering: Any, ledger: _Ledger) -> None:
    if not isinstance(tampering, dict) or tampering.get("implemented") is False:
        ledger.add(
            "tampering_unavailable",
            "Tampering forensics did not run — document integrity unknown",
            signal="Tampering unavailable",
        )
        return
    verdict = _verdict_of(tampering, "verdict")
    if verdict == "likely_manipulated":
        ledger.add(
            "tampering_likely",
            "Forensic detectors agree: document image likely manipulated",
            signal="Tampering: likely manipulated",
        )
    elif verdict == "suspicious":
        ledger.add(
            "tampering_suspicious",
            "Forensic detectors flag possible image manipulation",
            signal="Tampering: suspicious",
        )
    elif verdict == "inconclusive":
        ledger.add(
            "tampering_inconclusive",
            "Tampering analysis could not complete reliably — integrity unknown",
            signal="Tampering inconclusive",
        )
    score100 = tampering.get("risk_score")
    if isinstance(score100, (int, float)) and score100 > 0:
        share = _clamp100(score100) / 100.0 * WEIGHTS["tampering_score_share_max"]
        floor = {
            "likely_manipulated": WEIGHTS["tampering_likely"],
            "suspicious": WEIGHTS["tampering_suspicious"],
            "inconclusive": WEIGHTS["tampering_inconclusive"],
        }.get(verdict, 0.0)
        incremental = max(0.0, share - floor)
        if incremental >= 0.5:
            ledger.points += incremental
            ledger.contributions.append(
                {
                    "signal": "Tampering fused score",
                    "points": round(incremental, 1),
                    "details": f"forensic risk {float(score100):.0f}/100 (proportional share)",
                }
            )


def _face_signal(face: Any, ledger: _Ledger) -> None:
    if not isinstance(face, dict) or face.get("implemented") is False:
        ledger.add(
            "face_unavailable",
            "Face verification did not run — portrait consistency unknown",
            signal="Face unavailable",
        )
        return
    status = _verdict_of(face, "match_status")
    conf_raw = face.get("confidence")
    conf = _clamp100(conf_raw) / 100.0 if isinstance(conf_raw, (int, float)) and conf_raw > 1 else (
        max(0.0, min(1.0, float(conf_raw))) if isinstance(conf_raw, (int, float)) else None
    )
    if status == "NO_MATCH":
        ledger.add(
            "face_no_match",
            "Face verification mismatch: document portrait and presented person are not consistent",
            signal="Face: NO_MATCH",
        )
    elif status == "INCONCLUSIVE":
        ledger.add(
            "face_inconclusive",
            "Face verification inconclusive — could not compare portraits reliably",
            signal="Face inconclusive",
        )
    elif status == "MATCH":
        if conf is not None and conf < FACE_CONF_HIGH:
            ledger.add(
                "face_low_confidence_match",
                f"Face match holds but with low confidence ({conf:.0%})",
                signal="Face low-confidence match",
            )
    else:
        ledger.add(
            "face_inconclusive",
            "Face engine produced no verdict",
            signal="Face verdict missing",
        )


def _expiry_signal(ocr_rows: list[dict[str, Any]], ledger: _Ledger) -> None:
    """Document expiry: expired / expiring-soon / unparseable (Step 8 category)."""
    settings = get_settings()
    window = max(0, int(getattr(settings, "risk_expiring_soon_days", 90)))
    row = next((r for r in ocr_rows if str(r.get("name", "")).lower() in {"expiry_date", "date_of_expiry"}), None)
    if row is None or not _value_of(row):
        return  # no expiry field at all: missing-field logic covers the gap
    raw = _value_of(row).translate(_DATE_DIGIT_FIXES)
    parsed: date | None = None
    if _ISO_DATE_RE.match(raw):
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            parsed = None
    else:
        for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
    if parsed is None:
        ledger.add(
            "expiry_unparseable",
            f"Expiry date present but unreadable ('{_value_of(row)[:20]}')",
            signal="Expiry unparseable",
        )
        return
    today = date.today()
    days_left = (parsed - today).days
    if days_left < 0:
        ledger.add(
            "expired",
            f"Document EXPIRED on {parsed.isoformat()} ({-days_left} days ago)",
            signal="Document expired",
        )
    elif days_left <= window:
        ledger.add(
            "expiring_soon",
            f"Document expires within {days_left} day(s) ({parsed.isoformat()})",
            signal="Expiring soon",
        )


def _missing_fields_signal(ocr_rows: list[dict[str, Any]], doc_type: str, ledger: _Ledger) -> None:
    """Required identity-field groups absent from the OCR output (Step 8).

    Fields are matched as GROUPS so any acceptable alias satisfies the
    requirement (``passport_number`` OR ``document_number``; ``full_name``
    OR ``surname``+``given_names`` ...). The number group expands with the
    doc-type-specific aliases.
    """
    dtype = (doc_type or "").lower()
    number_aliases = {
        "passport": "passport_number", "visa": "visa_number",
        "national_id": "national_id_number", "driving_license": "license_number",
        "permit": "permit_number",
    }
    number_group = {"document_number", number_aliases.get(dtype, "national_id_number")}
    groups: dict[str, set[str]] = {
        "name": {"full_name", "surname", "given_names"},
        "date_of_birth": {"date_of_birth", "dob"},
        "expiry_date": {"expiry_date", "date_of_expiry"},
        "document_number": number_group,
    }
    if dtype == "passport":
        groups["nationality"] = {"nationality", "issuing_country"}
    present = {
        str(r.get("name", "")).lower()
        for r in ocr_rows
        if _value_of(r)
    }
    missing = sorted(
        group for group, aliases in groups.items()
        if not any(alias in present for alias in aliases)
    )
    if missing:
        ledger.capped_add(
            "missing_field_each",
            WEIGHTS["missing_field_each"],
            len(missing),
            lambda n: f"Required field group(s) missing from the document: {', '.join(missing[:5])}"
            + (f" (+{n - 5} more)" if n > 5 else ""),
            signal="Missing required fields",
            cap=WEIGHTS["missing_fields_cap"],
        )


def _suspicious_patterns_signal(ocr_rows: list[dict[str, Any]], ledger: _Ledger) -> None:
    """Coarse, explainable value-pattern heuristics (Step 8 category).

    Deliberately conservative: each detector fires on *specific* observable
    defects, never on "looks weird". All points share one cap.
    """
    points = 0.0

    def add(pts: float, reason: str, details: str) -> None:
        nonlocal points
        if pts <= 0:
            return
        points += pts
        ledger.reasons.append(reason)
        ledger.contributions.append(
            {"signal": "Suspicious field pattern", "points": round(pts, 1), "details": details}
        )

    filled = [r for r in ocr_rows if _value_of(r)]
    for row in filled:
        name = str(row.get("name", ""))
        value = _value_of(row)
        low_value = value.lower()
        lname = name.lower()

        # 1. Embedded whitespace / zero-width characters inside compact values.
        if lname in NUMBER_FIELD_HINTS and re.search(r"[\s\u200b\u200c\u200f]", value):
            add(
                WEIGHTS["suspicious_embedded_whitespace"],
                f"Suspicious spacing characters inside {name}",
                f"value='{value[:24]}'",
            )

        # 2. Letters that look like digits (O/0, I/1, S/5 ...) inside document
        #    numbers. Only the letters that are RARE in real document numbers
        #    are flagged — common ones like 'L' would fire on nearly every
        #    passport and train reviewers to ignore the warning.
        if lname in NUMBER_FIELD_HINTS and any(ch in _SUSPECT_CHARS_IN_NUMBERS for ch in low_value):
            add(
                WEIGHTS["suspicious_char_confusion"],
                f"Character commonly misread as a digit (O/0, I/1, S/5 …) inside {name} — verify character-by-character",
                f"value='{value[:24]}'",
            )

        # 3. Charset mismatches: digits inside name-ish fields, letters inside
        #    number-ish fields.
        if lname in {"full_name", "surname", "given_names"} and re.search(r"\d", value):
            add(
                WEIGHTS["suspicious_charset_mismatch"],
                f"Digits found inside {name} — inconsistent with a personal name",
                f"value='{value[:24]}'",
            )

    # 4. The same value filling two different number fields.
    seen: dict[str, str] = {}
    for row in filled:
        lname = str(row.get("name", "")).lower()
        if lname in NUMBER_FIELD_HINTS:
            v = _value_of(row).upper()
            if v in seen and seen[v] != lname:
                add(
                    WEIGHTS["suspicious_duplicate_number"],
                    f"Same document number value present as both {seen[v]} and {lname}",
                    f"value='{v[:24]}'",
                )
            seen[v] = lname

    cap = WEIGHTS["suspicious_patterns_cap"]
    if points > cap:
        overflow = points - cap
        points = cap
        # Keep the ledger honest about the clamp.
        ledger.contributions.append(
            {"signal": "Suspicious-pattern cap applied", "points": round(-overflow, 1), "details": f"clamped to {cap:.0f}"}
        )
    ledger.points += points


def _metadata_signal(tampering: Any, ledger: _Ledger) -> None:
    """Metadata anomalies surfaced by the tampering stage's metadata detector."""
    if not isinstance(tampering, dict):
        return
    points = 0.0

    def add(pts: float, reason: str, details: str) -> None:
        nonlocal points
        if pts <= 0:
            return
        points += pts
        ledger.reasons.append(reason)
        ledger.contributions.append(
            {"signal": "Metadata anomaly", "points": round(pts, 1), "details": details}
        )

    indicators = tampering.get("indicators") or []
    if not isinstance(indicators, list):
        indicators = []
    for ind in indicators:
        if not isinstance(ind, dict) or ind.get("type") != "metadata_anomaly":
            continue
        severity = ind.get("severity")
        note = str(ind.get("note", ""))[:120]
        if severity == "medium":
            add(
                WEIGHTS["metadata_editor_fingerprint"],
                f"Metadata anomaly: {note}",
                "photo-editing software fingerprint",
            )
        elif severity == "low":
            add(
                WEIGHTS["metadata_exif_stripped"],
                f"Metadata anomaly: {note}",
                "weak provenance signal",
            )
    summary = tampering.get("metadata_summary") or {}
    if isinstance(summary, dict) and summary.get("has_xmp") and not summary.get("has_exif"):
        add(
            WEIGHTS["metadata_software_without_camera"],
            "XMP packet present but EXIF absent — metadata partially stripped",
            "metadata_summary",
        )
    cap = WEIGHTS["metadata_cap"]
    if points > cap:
        overflow = points - cap
        points = cap
        ledger.contributions.append(
            {"signal": "Metadata cap applied", "points": round(-overflow, 1), "details": f"clamped to {cap:.0f}"}
        )
    ledger.points += points


def _stage_outcomes_signal(stage_results: list[dict[str, Any]] | None, ledger: _Ledger) -> None:
    entries: list[str] = []
    for r in stage_results or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("stage", "stage"))
        status = r.get("status")
        if status == "error":
            entries.append(f"stage '{name}' crashed")
        elif status == "skipped":
            entries.append(f"stage '{name}' skipped (missing inputs)")
    if entries:
        cap = WEIGHTS["stage_outcomes_cap"]
        pts = min(cap, len(entries) * WEIGHTS["stage_error"])
        ledger.points += pts
        ledger.reasons.append("Pipeline incomplete: " + "; ".join(entries[:4]) + ("…" if len(entries) > 4 else ""))
        ledger.contributions.append(
            {"signal": "Degraded pipeline stages", "points": round(pts, 1), "details": f"{len(entries)} stage(s)"}
        )


# ---------------------------------------------------------------------------
# Level + confidence
# ---------------------------------------------------------------------------
def _level(score: float) -> str:
    settings = get_settings()
    if score <= settings.risk_band_low_max:
        return LEVEL_LOW
    if score <= settings.risk_band_medium_max:
        return LEVEL_MEDIUM
    if score <= settings.risk_band_high_max:
        return LEVEL_HIGH
    return LEVEL_CRITICAL


def _confidence(
    score: float,
    stage_results: list[dict[str, Any]] | None,
    stage_outputs: dict[str, Any] | None,
) -> float:
    """Confidence in the *level verdict itself*, not in the document."""
    degraded = sum(
        1 for r in (stage_results or [])
        if isinstance(r, dict) and r.get("status") in {"error", "skipped", "not_implemented"}
    )
    conf = CONF_BASE - min(CONF_DEGRADED_CAP, degraded * CONF_PER_DEGRADED_STAGE)
    if stage_results is not None:
        any_engine_ran = any(
            isinstance(r, dict) and r.get("status") == "ok" and r.get("stage") != "risk_assessment"
            for r in stage_results
        )
    else:
        any_engine_ran = bool(stage_outputs) and any(
            isinstance(v, dict) and v.get("implemented") is not False for v in stage_outputs.values()
        )
    if not any_engine_ran:
        conf = min(conf, CONF_ERROR_FLOOR)
    settings = get_settings()
    for edge in (settings.risk_band_low_max, settings.risk_band_medium_max, settings.risk_band_high_max):
        if abs(score - edge) <= 2:
            conf -= CONF_BAND_EDGE_PENALTY
            break
    return round(max(0.0, min(1.0, conf)), 3)


def _review_reasons(
    score: float,
    level: str,
    ledger: _Ledger,
    stage_results: list[dict[str, Any]] | None,
) -> list[str]:
    settings = get_settings()
    reasons: list[str] = []
    if score >= max(settings.risk_band_medium_max, REVIEW_SCORE_MIN) + 1:
        reasons.append(f"risk score {score:.0f} above the MEDIUM band ceiling")
    if level in {LEVEL_MEDIUM, LEVEL_HIGH, LEVEL_CRITICAL}:
        reasons.append(f"risk level {level} routes to review by policy")
    degraded = [
        str(r.get("stage")) for r in (stage_results or [])
        if isinstance(r, dict) and r.get("status") in {"error", "skipped", "not_implemented"}
    ]
    if degraded:
        reasons.append("degraded pipeline stages: " + ", ".join(degraded[:4]))
    if not reasons:
        reasons.append("low risk — advisory only; human confirmation still required by policy")
    return reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def assess_risk(
    stage_outputs: dict[str, Any],
    stage_results: list[dict[str, Any]] | None = None,
    *,
    doc_type: str | None = None,
) -> dict[str, Any]:
    """Fuse all stage outputs into the Step-8 contract payload.

    ``stage_outputs`` carries the ctx-published blocks (``ocr_fields``,
    ``validation``, ``tampering``, ``face_verification``). ``stage_results``
    is the per-stage outcome list; ``doc_type`` gates the missing-field
    rules. Never raises for malformed inputs — junk degrades to honest
    "unknown" signals.
    """
    started = time.perf_counter()
    if not isinstance(stage_outputs, dict):
        raise RiskAssessmentError("stage_outputs must be a dict")

    ledger = _Ledger()
    ocr_rows = _field_rows(stage_outputs.get("ocr_fields"))
    if doc_type is None:
        doc_type = str(stage_outputs.get("doc_type_detected") or stage_outputs.get("doc_type_hint") or "")

    # --- the eight Step-8 signal groups -------------------------------------
    _ocr_signal(stage_outputs.get("ocr_fields"), ledger)
    _validation_signal(stage_outputs.get("validation"), ledger)
    _tampering_signal(stage_outputs.get("tampering"), ledger)
    _face_signal(stage_outputs.get("face_verification"), ledger)
    _expiry_signal(ocr_rows, ledger)
    _missing_fields_signal(ocr_rows, doc_type, ledger)
    _suspicious_patterns_signal(ocr_rows, ledger)
    _metadata_signal(stage_outputs.get("tampering"), ledger)
    _stage_outcomes_signal(stage_results, ledger)

    # Step-14 compound escalation: the impostor pattern. A face NO_MATCH on a
    # document whose validation is fully clean and whose tampering analysis
    # shows no manipulation means the paper is fine but the PRESENTER is
    # wrong — route it to HIGH rather than the flat 35-point MEDIUM.
    face = stage_outputs.get("face_verification") or {}
    validation = stage_outputs.get("validation") or {}
    tampering = stage_outputs.get("tampering") or {}
    if (
        _verdict_of(face, "match_status") == "NO_MATCH"
        and not (validation.get("failures") or [])
        and _verdict_of(tampering, "verdict") in {"no_obvious_manipulation", "inconclusive"}
    ):
        ledger.add(
            "face_no_match_clean_document",
            "Impostor pattern: face mismatch on an otherwise fully clean document "
            "— the presented person is likely not the document holder",
            signal="Face: NO_MATCH on clean document",
        )

    score = round(min(100.0, max(0.0, ledger.points)), 1)
    level = _level(score)
    confidence = _confidence(score, stage_results, stage_outputs)

    # Routing reasons are additive; human_review_required is ALWAYS true —
    # this is decision-support, the human makes the decision.
    routing_reasons = _review_reasons(score, level, ledger, stage_results)
    human_review_required = True
    review_score_floor = REVIEW_SCORE_MIN

    settings = get_settings()
    explanation = (
        f"Level {level} from {score:.0f}/100 "
        f"(bands: LOW≤{settings.risk_band_low_max}, MEDIUM≤{settings.risk_band_medium_max}, "
        f"HIGH≤{settings.risk_band_high_max}). "
        + ("Top drivers: " + "; ".join(ledger.reasons[:3]) + ". " if ledger.reasons else "No adverse signals. ")
        + "Every point is reconstructible from the contribution table."
    )

    return {
        "implemented": True,
        "engine": "weighted-rules-v2",
        # --- Step-8 contract (spec) -----------------------------------------
        "risk_score": int(round(score)),
        "risk_level": level,
        "reasons": ledger.reasons,
        "human_review_required": human_review_required,
        # --- richer payload for the UI / audit -------------------------------
        "score": int(round(score)),          # alias, 0-100
        "level": level,                       # alias
        "band": level.lower(),                # lowercase alias (DB stores band)
        "confidence": confidence,
        "contributions": ledger.contributions,
        # Nested block following the same pattern as tampering/face payloads;
        # the pipeline publishes this into the ctx and the API persists from it.
        "risk": {
            "score": int(round(score)),
            "level": level,
            "band": level.lower(),
            "reasons": ledger.reasons,
            "contributions": ledger.contributions,
            "confidence": confidence,
            "human_review_required": human_review_required,
            "routing_reasons": routing_reasons,
        },
        "routing_reasons": routing_reasons,
        "bands": {
            "low_max": settings.risk_band_low_max,
            "medium_max": settings.risk_band_medium_max,
            "high_max": settings.risk_band_high_max,
            "note": "configurable via APP_RISK_BAND_* — policy, not fact",
        },
        "review_score_floor": review_score_floor,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "disclaimer": (
            "Risk score is a deterministic weighted fusion of advisory AI "
            "signals on a 0-100 scale — NOT a legal determination and never "
            "an automatic accept/reject. Band thresholds are deployment "
            "policy, not universal facts. All screening outcomes require an "
            "authorized human decision."
        ),
    }


__all__ = [
    "LEVELS",
    "LEVEL_CRITICAL",
    "LEVEL_HIGH",
    "LEVEL_LOW",
    "LEVEL_MEDIUM",
    "RiskAssessmentError",
    "assess_risk",
]
