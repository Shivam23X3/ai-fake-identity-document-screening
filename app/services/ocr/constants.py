"""Shared types + constants for the OCR module.

Field naming is stable and frontend-consumed: keep renames in sync with
``frontend/src/types.ts`` and ``AnalysisPanels.tsx``.
"""
from __future__ import annotations

import re
from typing import Final

# Document types the extractor knows how to handle. The hint accepted by
# the upload endpoint is the same set; OCR may also detect these.
DOC_TYPES: Final[frozenset[str]] = frozenset(
    {"passport", "visa", "national_id", "driving_license", "permit", "unknown"}
)

# Confidence thresholds (tunable via Settings).
DEFAULT_FIELD_THRESHOLD: Final[float] = 0.75   # below ⇒ field flagged for review
DEFAULT_DOC_THRESHOLD: Final[float] = 0.70     # below ⇒ whole extraction flagged
MIN_OVERALL_FOR_AUTO_OK: Final[float] = 0.90   # below ⇒ run routed to human review

# Canonical field names per document type (order = display order).
PASSPORT_FIELDS: Final[tuple[str, ...]] = (
    "full_name",
    "passport_number",
    "nationality",
    "date_of_birth",
    "expiry_date",
    "gender",
)
VISA_FIELDS: Final[tuple[str, ...]] = (
    "visa_number",
    "visa_type",
    "entry_validity",
    "stay_duration",
)

# Fields that are dates (formatting/normalization + date sanity checks).
DATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"date_of_birth", "expiry_date", "issue_date", "date_of_issue"}
)

_OCR_NOISE_CHARS: Final[dict[str, str]] = {
    "0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B",
    "!": "I", "|": "I", "(": "I", ")": "I", "[": "I", "]": "I",
    "{": "I", "}": "I", "“": '"', "”": '"', "’": "'",
}

# Tokens that look like dates in free text: 12 May 1990 / 12.05.1990 /
# 1990-05-12 / 12/05/1990.
_DATE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2}[ ./\-][A-Za-z0-9]{3,9}[ ./\-]\d{2,4}"
    r"|\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}"
    r"|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})\b"
)

# OCR commonly misreads these inside alnum document numbers.
AMBIGUOUS_OCR_CHARS: Final[frozenset[str]] = frozenset("O0Qo1lI|5S2Z8B6G")


_DIGIT_FIXES: Final[dict[str, str]] = {
    "O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
    "Z": "2", "S": "5", "B": "8", "G": "6",
}


def fix_ocr_char(token: str, position: int, mode: str) -> str:
    """Return the corrected character for ``token[position]``.

    ``mode``: ``alpha`` (letters), ``digit`` (numbers) or ``alnum``
    (decide by neighboring chars). Returns the original char unchanged
    when no confusable mapping applies.
    """
    if position < 0 or position >= len(token):
        return ""
    ch = token[position]
    if mode == "alpha" and ch.isdigit():
        return _OCR_NOISE_CHARS.get(ch, ch)
    if mode == "digit" and ch.isalpha():
        return _DIGIT_FIXES.get(ch.upper(), ch)
    if mode == "alnum":
        prev_ch = token[position - 1] if position > 0 else ""
        next_ch = token[position + 1] if position + 1 < len(token) else ""
        if prev_ch.isdigit() or next_ch.isdigit():
            return fix_ocr_char(token, position, "digit")
        if prev_ch.isalpha() or next_ch.isalpha():
            return fix_ocr_char(token, position, "alpha")
    return ch


def date_token_spans(text: str) -> list[tuple[int, int]]:
    """Spans of substrings that look like dates in free text."""
    return [(m.start(), m.end()) for m in _DATE_TOKEN_RE.finditer(text)]


def has_ambiguous_chars(token: str) -> bool:
    """True when a token contains chars OCR commonly confuses."""
    return any(ch in AMBIGUOUS_OCR_CHARS for ch in token)
