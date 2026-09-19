"""Text cleanup for OCR output.

Normalizes OCR noise without destroying signal: per-line character fixes,
whitespace normalization, and de-ligaturing. Keeps a record of what changed
so confidence scoring can penalize heavily-corrected lines.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.services.ocr.constants import fix_ocr_char

# Common OCR substitutions seen on document fonts.
_LINE_FIXES: dict[str, str] = {
    "«": "<",
    "»": ">",
    "‘": "'",
    "’": "'",
    "`": "'",
    "“": '"',
    "”": '"',
    "—": "-",
    "–": "-",
    "•": "",
    "l": "l",  # no-op guard
}

# MRZ lines: uppercase A-Z, digits and '<' only.
_MRZ_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<")


@dataclass
class CleanupReport:
    """What the cleaner changed, for confidence accounting."""

    original_lines: list[str]
    cleaned_lines: list[str]
    changed_lines: int
    removed_chars: int


def normalize_line(line: str) -> str:
    """Unicode-normalize and apply common OCR substitutions."""
    line = unicodedata.normalize("NFKC", line)
    for bad, good in _LINE_FIXES.items():
        line = line.replace(bad, good)
    return line


def clean_general(line: str) -> tuple[str, int]:
    """Clean a normal (non-MRZ) text line. Returns (line, fix_count)."""
    original = line
    line = normalize_line(line)
    line = " ".join(line.split())  # collapse whitespace
    fixes = 0 if line == original else 1
    return line, fixes


def clean_mrz_candidate(line: str) -> tuple[str, int]:
    """Clean a line that looks like MRZ: uppercase, strip noise, fix '<'.

    Returns (cleaned_line, fix_count). Lines that cannot plausibly be MRZ
    are returned unchanged with fix_count 0.
    """
    candidate = normalize_line(line).upper().replace(" ", "")
    if len(candidate) < 20:  # real MRZ lines are 36/44 chars
        return line, 0
    fixes = 0
    chars: list[str] = []
    for ch in candidate:
        if ch in _MRZ_ALLOWED:
            chars.append(ch)
        elif ch in {"!", "|", "(", ")", "[", "]", "{", "}", "§", "°"}:
            chars.append("<" if ch in {"(", ")", "[", "]", "{", "}"} else "I")
            fixes += 1
        elif ch.isalpha():
            chars.append(ch.upper())
        elif ch.isdigit():
            chars.append(ch)
        else:
            chars.append("<")  # unknown symbol on an MRZ line is usually '<'
            fixes += 1
    cleaned = "".join(chars)
    return (cleaned, fixes) if cleaned != line else (line, fixes)


def looks_like_mrz(line: str) -> bool:
    """Heuristic: long, mostly-uppercase, contains '<' fillers."""
    stripped = line.strip()
    if len(stripped) < 28:
        return False
    alpha = sum(ch.isalpha() or ch == "<" for ch in stripped)
    fillers = stripped.count("<")
    return alpha / len(stripped) > 0.8 and fillers >= 3


def cleanup_lines(lines: list[str]) -> CleanupReport:
    """Clean all lines; MRZ-looking lines get MRZ-specific treatment."""
    cleaned: list[str] = []
    changed = 0
    removed = 0
    for line in lines:
        if looks_like_mrz(line):
            new_line, fixes = clean_mrz_candidate(line)
        else:
            new_line, fixes = clean_general(line)
        removed += abs(len(line) - len(new_line))
        changed += fixes
        cleaned.append(new_line)
    return CleanupReport(
        original_lines=list(lines),
        cleaned_lines=cleaned,
        changed_lines=changed,
        removed_chars=removed,
    )


def apply_positional_fixes(token: str, mode: str) -> str:
    """Fix OCR-confusable chars position-wise (see constants.fix_ocr_char)."""
    out = token
    for pos in range(len(out)):
        out = fix_ocr_char(out, pos, mode)
    return out
