"""Ruleset definitions + registry — the configuration layer.

A ``Ruleset`` binds a document type (optionally per issuing country) to
an ordered list of rules. The engine looks rulesets up here; adding a
country or document type later means ``register_ruleset(...)`` with new
rule definitions — no engine changes.

HONESTY: the bundled rulesets are GENERIC, simplified checks (MRZ check
digits, ISO dates, common formats). They intentionally do NOT claim to
encode any real country's full document rules. Register a country-
specific ruleset to override them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.validation.rules import (
    Rule,
    check_digit_field,
    date_fields,
    dob_in_past,
    expiry_validity,
    mrz_composite_digest,
    mrz_cross_consistency,
    mrz_doc_code_consistency,
    mrz_presence,
    regex_format,
    required_fields,
    visa_entry_validity,
    visa_stay_duration,
)


@dataclass
class RuleSpec:
    """A named rule reference inside a ruleset (rule callables carry ids)."""

    rule: Rule
    name: str | None = None  # optional human label override


@dataclass
class Ruleset:
    """Ordered rules for one document type (optionally country-specific)."""

    doc_type: str
    rules: list[Rule] = field(default_factory=list)
    country: str | None = None          # None = generic for the doc type
    description: str = ""
    #: Fields the ruleset considers mandatory (informational; the rules
    #: themselves decide the checks).
    required: list[str] = field(default_factory=list)
    #: Ruleset id (None = generic, simplified rules — disclosed in output).
    country_real_rules: bool = False


def register_ruleset(ruleset: Ruleset) -> None:
    """Register/replace a ruleset. Key = (country or '*', doc_type)."""
    key = ((ruleset.country or "*").upper(), ruleset.doc_type.lower())
    _RULESETS[key] = ruleset


def get_ruleset(doc_type: str, country: str | None = None) -> Ruleset | None:
    """Most specific ruleset wins: (country, doc_type) → ('*', doc_type).

    Unknown doc types fall back to the 'unknown' ruleset (generic checks).
    """
    country_key = (country or "*").upper()
    doc_key = (doc_type or "unknown").lower()
    ruleset = _RULESETS.get((country_key, doc_key)) or _RULESETS.get(("*", doc_key))
    if ruleset is None:
        ruleset = _RULESETS.get(("*", "unknown"))
    return ruleset


# ---------------------------------------------------------------------------
# Bundled rulesets (generic, simplified — see honesty note above)
# ---------------------------------------------------------------------------
PASSPORT_RULESET = Ruleset(
    doc_type="passport",
    description=(
        "Generic passport checks: required fields, formats, dates, expiry, "
        "MRZ check digits + cross-consistency. NOT any country's official rules."
    ),
    required=["full_name", "passport_number", "date_of_birth", "expiry_date", "nationality"],
    rules=[
        mrz_presence(expected=True),
        mrz_doc_code_consistency(),
        required_fields("full_name", "passport_number", "date_of_birth", "expiry_date", "nationality"),
        regex_format("passport_number", r"[A-Z]{1,2}[A-Z0-9]{6,9}",
                     "starts with 1-2 letters + 6-9 alphanumeric chars (common ICAO 9303 shape)"),
        regex_format("nationality", r"[A-Z]{3}",
                     "3-letter ICAO country code"),
        date_fields("date_of_birth", "expiry_date"),
        dob_in_past("date_of_birth"),
        expiry_validity("expiry_date"),
        check_digit_field("passport_number", "Passport number"),
        check_digit_field("date_of_birth", "Date of birth"),
        check_digit_field("expiry_date", "Expiry date"),
        mrz_composite_digest(),
        mrz_cross_consistency([
            ("passport_number", "passport_number"),
            ("nationality", "nationality"),
            ("date_of_birth", "date_of_birth"),
            ("expiry_date", "expiry_date"),
        ]),
    ],
)

VISA_RULESET = Ruleset(
    doc_type="visa",
    description=(
        "Generic visa checks: number format, type, entry validity, stay "
        "duration, required fields, MRZ consistency. NOT any country's official rules."
    ),
    required=["visa_number", "full_name", "date_of_birth", "expiry_date", "entry_validity"],
    rules=[
        mrz_presence(expected=True),
        mrz_doc_code_consistency(),
        required_fields("visa_number", "full_name", "date_of_birth", "expiry_date", "entry_validity"),
        regex_format("visa_number", r"[A-Z]{1,2}[A-Z0-9]{6,9}",
                     "starts with letters + 6-9 alphanumeric chars (common visa-number shape)"),
        # Visa type is machine-unreadable (visual zone only); a hard FAIL on
        # OCR's inability would be dishonest — keep it advisory.
        regex_format("visa_type", r"[A-Z0-9][A-Z0-9 /\\-]{1,23}",
                     "letters/digits + optional category suffix",
                     severity_fail="WARNING"),
        visa_entry_validity("entry_validity"),
        visa_stay_duration("stay_duration"),
        date_fields("date_of_birth", "expiry_date"),
        dob_in_past("date_of_birth"),
        expiry_validity("expiry_date"),
        check_digit_field("visa_number", "Visa number"),
        check_digit_field("date_of_birth", "Date of birth"),
        check_digit_field("expiry_date", "Expiry date"),
        mrz_composite_digest(),
        mrz_cross_consistency([
            ("visa_number", "visa_number"),
            ("date_of_birth", "date_of_birth"),
            ("expiry_date", "expiry_date"),
            ("entry_validity", "entry_validity"),
        ]),
    ],
)

# Generic fallback for doc types without a dedicated ruleset yet
# (national_id, driving_license, permit): MRZ/layout-agnostic basic checks.
GENERIC_RULESET = Ruleset(
    doc_type="unknown",
    description=(
        "Basic checks for document types without a dedicated ruleset: "
        "dates, expiry, DOB sanity, MRZ check digits when present."
    ),
    required=[],
    rules=[
        date_fields("date_of_birth", "expiry_date", "date_of_issue"),
        dob_in_past("date_of_birth"),
        expiry_validity("expiry_date"),
        check_digit_field("document_number", "Document number"),
        mrz_presence(expected=False),
    ],
)


_RULESETS: dict[tuple[str, str], Ruleset] = {}


def _register_bundled() -> None:
    register_ruleset(PASSPORT_RULESET)
    register_ruleset(VISA_RULESET)
    register_ruleset(GENERIC_RULESET)


_register_bundled()


def describe_rulesets() -> list[dict[str, Any]]:
    """Machine-readable ruleset inventory (for /pipeline/info or docs)."""
    out = []
    for (country, doc_type), rs in sorted(_RULESETS.items()):
        out.append({
            "country": country,
            "doc_type": doc_type,
            "description": rs.description,
            "rules": [getattr(r, "__name__", repr(r)) for r in rs.rules],
            "required_fields": rs.required,
            "country_real_rules": rs.country_real_rules,
        })
    return out
