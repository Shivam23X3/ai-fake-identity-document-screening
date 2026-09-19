"""Field extraction from OCR output.

Strategy per document type:
1. MRZ first — machine-printed, fixed layout, has check digits. When an
   MRZ parses, its fields win over visual-zone guesses.
2. Visual zone fallback — labeled-field regexes over the cleaned text for
   documents without an MRZ (driving licenses, permits, some IDs).
3. Values the MRZ cannot provide (e.g. visa type) come from labeled
   fields or header detection.

Every field carries: value, source (mrz|visual_zone|header), confidence
0..1, flagged (low confidence) and notes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from typing import Any

from app.services.ocr.cleanup import normalize_line
from app.services.ocr.constants import (
    DATE_FIELDS,
    PASSPORT_FIELDS,
    VISA_FIELDS,
    date_token_spans,
    fix_ocr_char,
)
from app.services.ocr.engine import OcrOutput
from app.services.ocr.mrz import MrzResult

logger = logging.getLogger(__name__)

FIELD_THRESHOLD = 0.75  # below ⇒ flagged for human review


@dataclass
class ExtractedField:
    """One document field with provenance + confidence."""

    name: str
    value: str | None
    confidence: float
    source: str            # "mrz" | "visual_zone" | "header"
    flagged: bool = False
    check_digit_ok: bool | None = None
    notes: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "flagged": self.flagged,
        }
        if self.check_digit_ok is not None:
            out["check_digit_ok"] = self.check_digit_ok
        if self.notes:
            out["notes"] = self.notes
        return out


@dataclass
class ExtractionResult:
    """All fields for one document."""

    document_type: str
    fields: dict[str, ExtractedField]
    mrz: MrzResult | None = None
    mrz_raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_type": self.document_type,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "mrz_format": self.mrz.format if self.mrz else None,
            "mrz_fields": self.mrz.field_dict() if self.mrz else None,
            "mrz_raw": self.mrz_raw,
        }

    def overall_confidence(self) -> float | None:
        confidences = [f.confidence for f in self.fields.values() if f.value is not None]
        if not confidences:
            return None
        return sum(confidences) / len(confidences)

    def any_flagged(self) -> bool:
        return any(f.flagged for f in self.fields.values())


# ---------------------------------------------------------------------------
# Visual-zone regexes (labeled fields)
# ---------------------------------------------------------------------------
# Labels are matched leniently: OCR often reads ':' as ';' or drops it.
def _label_regex(label: str, value_pattern: str) -> re.Pattern[str]:
    label_spaced = r"[\s.\-]*".join(re.escape(ch) for ch in label.upper())
    return re.compile(label_spaced + r"[\s.:=;_\-]*(" + value_pattern + r")", re.IGNORECASE)


_VISUAL_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "passport_number": [
        re.compile(r"\b([A-Z]{1,2}[0-9]{6,9})\b"),
    ],
    "visa_number": [
        _label_regex("visa no", r"[A-Z0-9]{6,12}"),
        _label_regex("visa number", r"[A-Z0-9]{6,12}"),
    ],
    "nationality": [
        _label_regex("nationality", r"[A-Z][A-Z ]{2,20}"),
    ],
    "surname": [
        _label_regex(r"surname", r"[A-Z][A-Z '\-]{1,30}"),
        _label_regex(r"last name", r"[A-Z][A-Z '\-]{1,30}"),
    ],
    "given_names": [
        _label_regex(r"given names?", r"[A-Z][A-Z '\-]{1,30}"),
        _label_regex(r"first name", r"[A-Z][A-Z '\-]{1,30}"),
    ],
    "gender": [
        _label_regex(r"sex", r"(M|F|MALE|FEMALE)\b"),
        _label_regex(r"gender", r"(M|F|MALE|FEMALE)\b"),
    ],
    "visa_type": [
        _label_regex(r"visa type", r"[A-Z0-9 /\-]{2,24}"),
        _label_regex(r"visa category", r"[A-Z0-9 /\-]{2,24}"),
    ],
    "stay_duration": [
        _label_regex(r"duration of stay", r"[0-9]{1,3}\s*(?:DAYS?|MONTHS?)"),
        _label_regex(r"stay duration", r"[0-9]{1,3}\s*(?:DAYS?|MONTHS?)"),
    ],
    "entry_validity": [
        _label_regex(r"entries", r"(MULTIPLE|SINGLE|M|S)"),
        _label_regex(r"entry", r"(MULTIPLE|SINGLE|M|S)"),
    ],
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _normalize_date(raw: str) -> str | None:
    """Normalize common date shapes to ISO; None when not parseable."""
    raw = raw.strip().replace(".", "-").replace("/", "-").replace(" ", "-")
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3,9})-(\d{4})", raw)
    if m:
        month = _MONTHS.get(m.group(2)[:3].upper())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{2})", raw)
    if m:
        d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            year = 2000 + yy if yy <= 49 else 1900 + yy
            return f"{year:04d}-{mo:02d}-{d:02d}"
    return None


def _dates_in_line(line: str) -> list[str]:
    """ISO-normalized date values found in a line, in order."""
    out = []
    for start, end in date_token_spans(line):
        iso = _normalize_date(line[start:end])
        if iso:
            out.append(iso)
    return out


def _alpha_conf(word_confidences: list[float]) -> float:
    """Mean confidence of the words supporting a value."""
    if not word_confidences:
        return 0.0
    return sum(word_confidences) / len(word_confidences)


def _clean_name(value: str) -> str:
    return re.sub(r"[^A-Za-z '\-]", "", value).strip()


def _fix_alnum(token: str) -> str:
    out = token
    for i in range(len(out)):
        out = fix_ocr_char(out, i, "alnum")
    return out


class FieldExtractor:
    """Extracts structured fields for one document type from OCR output."""

    def __init__(self, doc_type: str) -> None:
        self.doc_type = doc_type

    # -- public ------------------------------------------------------------
    def extract(
        self,
        ocr: OcrOutput,
        mrz: MrzResult | None,
        doc_type_detected: str | None,
    ) -> ExtractionResult:
        doc_type = self.doc_type if self.doc_type != "unknown" else (doc_type_detected or "unknown")
        if doc_type == "passport":
            fields = self._passport(ocr, mrz)
        elif doc_type == "visa":
            fields = self._visa(ocr, mrz)
        else:
            # National ID / driving license / permit: generic extraction —
            # MRZ dates/names if present, else labeled fields.
            fields = self._generic(ocr, mrz)
        result = ExtractionResult(
            document_type=doc_type,
            fields=fields,
            mrz=mrz,
            mrz_raw="\n".join(mrz.lines) if mrz else None,
        )
        self._flag_fields(result)
        return result

    # -- per-type ------------------------------------------------------------
    def _passport(self, ocr: OcrOutput, mrz: MrzResult | None) -> dict[str, ExtractedField]:
        fields: dict[str, ExtractedField] = {}
        if mrz:
            mf = mrz.fields
            name_value = " ".join(
                v for v in (mf["surname"].value, mf["given_names"].value) if v
            ).strip() or None
            fields["full_name"] = ExtractedField(
                "full_name", name_value,
                self._mrz_conf(ocr, mrz.lines[0], 5, 44, mrz=mrz),
                "mrz",
                check_digit_ok=None,
                notes=["from MRZ: surname + given names"],
            )
            fields["passport_number"] = ExtractedField(
                "passport_number", mf["passport_number"].value,
                self._mrz_conf(ocr, mrz.lines[1], 0, 9), "mrz",
                check_digit_ok=mf["passport_number"].check_digit_ok,
            )
            fields["nationality"] = ExtractedField(
                "nationality", mf["nationality"].value,
                self._mrz_conf(ocr, mrz.lines[1], 10, 13), "mrz",
            )
            fields["date_of_birth"] = ExtractedField(
                "date_of_birth", mf["date_of_birth"].value,
                self._mrz_conf(ocr, mrz.lines[1], 13, 19), "mrz",
                check_digit_ok=mf["date_of_birth"].check_digit_ok,
            )
            fields["expiry_date"] = ExtractedField(
                "expiry_date", mf["expiry_date"].value,
                self._mrz_conf(ocr, mrz.lines[1], 21, 27), "mrz",
                check_digit_ok=mf["expiry_date"].check_digit_ok,
            )
            fields["gender"] = ExtractedField(
                "gender", self._gender_full(mf["gender"].value),
                self._mrz_conf(ocr, mrz.lines[1], 20, 21), "mrz",
            )
            return fields
        return self._generic(ocr, None)

    def _visa(self, ocr: OcrOutput, mrz: MrzResult | None) -> dict[str, ExtractedField]:
        fields: dict[str, ExtractedField] = {}
        if mrz:
            mf = mrz.fields
            fields["visa_number"] = ExtractedField(
                "visa_number", mf["visa_number"].value,
                self._mrz_conf(ocr, mrz.lines[1], 0, 9), "mrz",
                check_digit_ok=mf["visa_number"].check_digit_ok,
            )
            fields["entry_validity"] = ExtractedField(
                "entry_validity", mf["entry_validity"].value,
                self._mrz_conf(ocr, mrz.lines[1], 28, 29), "mrz",
            )
            fields["stay_duration"] = ExtractedField(
                "stay_duration", mf["stay_duration"].value,
                self._mrz_conf(ocr, mrz.lines[1], 29, 31), "mrz",
                check_digit_ok=mf["stay_duration"].check_digit_ok,
            )
            # Visa type is NOT in the MRZ — visual zone only.
            vt = self._visual_field(ocr, "visa_type")
            if vt:
                fields["visa_type"] = vt
            else:
                fields["visa_type"] = ExtractedField(
                    "visa_type", None, 0.0, "visual_zone",
                    notes=["visa type is not machine-readable; enter manually"],
                )
            # Supplement: names/dates from MRZ (useful context).
            name_value = " ".join(
                v for v in (mf["surname"].value, mf["given_names"].value) if v
            ).strip() or None
            fields["full_name"] = ExtractedField(
                "full_name", name_value,
                self._mrz_conf(ocr, mrz.lines[0], 5, 36, mrz=mrz),
                "mrz",
            )
            fields["expiry_date"] = ExtractedField(
                "expiry_date", mf["expiry_date"].value,
                self._mrz_conf(ocr, mrz.lines[1], 21, 27), "mrz",
                check_digit_ok=mf["expiry_date"].check_digit_ok,
            )
            fields["date_of_birth"] = ExtractedField(
                "date_of_birth", mf["date_of_birth"].value,
                self._mrz_conf(ocr, mrz.lines[1], 13, 19), "mrz",
                check_digit_ok=mf["date_of_birth"].check_digit_ok,
            )
            return fields
        return self._generic(ocr, None)

    def _generic(self, ocr: OcrOutput, mrz: MrzResult | None) -> dict[str, ExtractedField]:
        fields: dict[str, ExtractedField] = {}
        if mrz:
            mf = mrz.fields
            name_value = " ".join(
                v for v in (mf["surname"].value, mf["given_names"].value) if v
            ).strip() or None
            if name_value:
                fields["full_name"] = ExtractedField(
                    "full_name", name_value,
                    self._mrz_conf(ocr, mrz.lines[0], 5, 44, mrz=mrz),
                    "mrz",
                )
            for key in ("document_number", "passport_number", "visa_number"):
                if key in mf and mf[key].value:
                    fields["document_number"] = ExtractedField(
                        "document_number", mf[key].value,
                        self._mrz_conf(ocr, mrz.lines[1], 0, 9), "mrz",
                        check_digit_ok=mf[key].check_digit_ok,
                    )
                    break
            for src_key, out_key in (("date_of_birth", "date_of_birth"), ("expiry_date", "expiry_date")):
                if src_key in mf and mf[src_key].value:
                    fields[out_key] = ExtractedField(
                        out_key, mf[src_key].value,
                        self._mrz_conf(ocr, mrz.lines[1], 13, 19), "mrz",
                        check_digit_ok=mf[src_key].check_digit_ok,
                    )
            if mf["gender"].value:
                fields["gender"] = ExtractedField(
                    "gender", self._gender_full(mf["gender"].value),
                    self._mrz_conf(ocr, mrz.lines[1], 20, 21), "mrz",
                )
            if "nationality" in mf and mf["nationality"].value:
                fields["nationality"] = ExtractedField(
                    "nationality", mf["nationality"].value,
                    self._mrz_conf(ocr, mrz.lines[1], 10, 13), "mrz",
                )
        # Visual-zone fallbacks fill anything still missing.
        visual_keys = ("full_name", "document_number", "date_of_birth",
                       "expiry_date", "gender", "nationality")
        for key in visual_keys:
            if key in fields and fields[key].value:
                continue
            if key == "full_name":
                viz = self._visual_field(ocr, "surname")
                viz2 = self._visual_field(ocr, "given_names")
                if viz and viz2:
                    fields[key] = ExtractedField(
                        "full_name", f"{viz.value} {viz2.value}".strip(),
                        min(viz.confidence, viz2.confidence), "visual_zone",
                    )
                elif viz:
                    fields[key] = viz
            elif key in {"date_of_birth", "expiry_date"}:
                viz = self._visual_date(ocr, key)
                if viz:
                    fields[key] = viz
            else:
                viz = self._visual_field(ocr, key)
                if viz:
                    fields[key] = viz
        return fields

    # -- MRZ confidence: mean of the OCR word confidences covering a span --
    def _mrz_conf(
        self, ocr: OcrOutput, mrz_line: str, start: int, end: int,
        mrz: MrzResult | None = None,
    ) -> float:
        """Confidence for MRZ[start:end] from the OCR words covering it.

        Falls back to a position-proportional prior when words cannot be
        aligned (e.g. MRZ re-typed by cleanup).
        """
        words = self._words_for_lines(ocr, [mrz_line])
        if not words and mrz is not None:
            words = self._words_for_lines(ocr, mrz.lines)
        line_chars = sum(len(w.text) for w in words) if words else 0
        if words and line_chars:
            # Map char span → word indices by cumulative length.
            total_len = sum(len(w.text) for w in words)
            frac_start, frac_end = start / 44, end / 44  # TD3 width prior
            idx_start = max(0, min(len(words) - 1, int(frac_start * len(words))))
            idx_end = max(idx_start + 1, min(len(words), int(frac_end * len(words))))
            selected = words[idx_start:idx_end]
            if selected:
                return _alpha_conf([w.confidence for w in selected])
        # Prior: middle-of-line fields OCR slightly worse than edges.
        center = (start + end) / 2 / 44
        return max(0.55, 0.92 - abs(center - 0.5) * 0.25)

    def _words_for_lines(self, ocr: OcrOutput, lines: list[str]) -> list[Any]:
        """OCR words belonging to the given MRZ lines (by line index)."""
        wanted: list[Any] = []
        for text in lines:
            norm = normalize_line(text).upper().replace(" ", "")
            for w in ocr.words:
                w_norm = normalize_line(w.text).upper().replace(" ", "")
                if w_norm and w_norm in norm:
                    wanted.append(w)
        # Deduplicate while preserving order.
        seen: set[int] = set()
        out = []
        for w in wanted:
            if id(w) not in seen:
                seen.add(id(w))
                out.append(w)
        return out

    # -- visual zone -------------------------------------------------------
    def _visual_field(self, ocr: OcrOutput, key: str) -> ExtractedField | None:
        patterns = _VISUAL_PATTERNS.get(key, [])
        for line_no, line in enumerate(ocr.lines):
            norm = normalize_line(line)
            for pat in patterns:
                m = pat.search(norm)
                if not m:
                    continue
                raw = m.group(1).strip(" .:-_")
                value = raw
                if key in {"gender"}:
                    value = self._gender_full(raw)
                elif key in {"full_name", "surname", "given_names"}:
                    value = _clean_name(raw)
                elif key in {"passport_number", "visa_number", "document_number"}:
                    value = _fix_alnum(raw.upper())
                elif key in {"visa_type", "nationality", "entry_validity", "stay_duration"}:
                    value = re.sub(r"\s+", " ", raw.upper()).strip(" .:-_")
                conf = _alpha_conf(
                    [w.confidence for w in ocr.words if w.line == line_no]
                ) or 0.5
                # Labeled visual fields are inherently less reliable.
                conf = min(conf, 0.9) * 0.9
                return ExtractedField(key, value or None, conf, "visual_zone")
        return None

    def _visual_date(self, ocr: OcrOutput, key: str) -> ExtractedField | None:
        label = "date of birth" if key == "date_of_birth" else "expiry"
        for line_no, line in enumerate(ocr.lines):
            norm = normalize_line(line)
            lowered = norm.lower()
            if label not in lowered and (key != "expiry_date" or "exp" not in lowered):
                continue
            dates = _dates_in_line(norm)
            if dates:
                conf = _alpha_conf([w.confidence for w in ocr.words if w.line == line_no]) or 0.5
                return ExtractedField(key, dates[0], min(conf * 0.9, 0.9), "visual_zone")
        # Unlabeled: if exactly one date-looking token in the whole doc, use it.
        all_dates = [d for line in ocr.lines for d in _dates_in_line(normalize_line(line))]
        if len(all_dates) == 1:
            return ExtractedField(key, all_dates[0], 0.45, "visual_zone",
                                  notes=["single date found without label"])
        return None

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _gender_full(value: str | None) -> str | None:
        if not value:
            return None
        v = value.strip().upper()
        if v.startswith("M") and v not in {"MISS"}:
            return "Male"
        if v.startswith("F"):
            return "Female"
        return None

    @staticmethod
    def _flag_fields(result: ExtractionResult) -> None:
        """Flag low-confidence fields + add sanity notes."""
        for f in result.fields.values():
            if f.value is None:
                f.flagged = True
                if not f.notes:
                    f.notes = ["field not found; enter/verify manually"]
                continue
            if f.confidence < FIELD_THRESHOLD:
                f.flagged = True
                f.notes.append(f"low OCR confidence ({f.confidence:.0%})")
            if f.name in DATE_FIELDS and f.value:
                # Sanity: DOB should be in the past; expiry in the future-ish.
                import datetime
                try:
                    d = datetime.date.fromisoformat(f.value)
                except ValueError:
                    f.flagged = True
                    f.notes.append("unparseable date")
                    continue
                today = datetime.date.today()
                if f.name == "date_of_birth" and d > today:
                    f.flagged = True
                    f.notes.append("birth date in the future")
                if f.name == "expiry_date" and d < today:
                    f.flagged = True
                    f.notes.append("document expired")
            if f.check_digit_ok is False:
                f.flagged = True
                f.notes.append("MRZ check digit mismatch")
