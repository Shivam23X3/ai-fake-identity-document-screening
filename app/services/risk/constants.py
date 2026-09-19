"""Shared vocabulary + weights for the risk-assessment module (Step 8).

Deterministic weighted rules fusion: every signal contributes a fixed,
documented number of points on a **0–100 scale**. There is no learning here
by design — every point must be explainable to a human reviewer
(`reasons[]` + `contributions[]` in the engine output).

Score scale: 0–100 (0 = no adverse signals, 100 = worst case). Individual
signals stay far below the CRITICAL floor so no single heuristic can
manufacture a critical verdict on its own.

Band thresholds are POLICY, not fact: the spec's 24/49/74 defaults are
configurable via ``APP_RISK_BAND_LOW_MAX`` / ``APP_RISK_BAND_MEDIUM_MAX`` /
``APP_RISK_BAND_HIGH_MAX`` (see ``app/core/config.py``).
"""
from __future__ import annotations

from typing import Final

# --- Vocabulary (spec: risk_level strings) ---------------------------------------
LEVEL_LOW: Final[str] = "LOW"
LEVEL_MEDIUM: Final[str] = "MEDIUM"
LEVEL_HIGH: Final[str] = "HIGH"
LEVEL_CRITICAL: Final[str] = "CRITICAL"

LEVELS: Final[frozenset[str]] = frozenset({LEVEL_LOW, LEVEL_MEDIUM, LEVEL_HIGH, LEVEL_CRITICAL})

# --- Score weights (points on the 0–100 scale) ------------------------------------
# Each entry: how many points adverse evidence of one kind adds. Conservative
# by design: any single signal stays below the MEDIUM ceiling (49) so one
# heuristic can never manufacture a HIGH verdict alone.
WEIGHTS: Final[dict[str, float]] = {
    # --- OCR quality -----------------------------------------------------------
    # OCR could not run or produced no fields at all: zero visibility.
    "ocr_unavailable": 15.0,
    # Per low-confidence field (capped at OCR_FIELD_CAP fields).
    "ocr_low_confidence_field": 3.0,
    # Per field the OCR module explicitly flagged (MRZ/check-digit notes).
    "ocr_flagged_field": 4.0,
    # --- Document validation ------------------------------------------------------
    # Validation engine unavailable ⇒ rule checks unknown.
    "validation_unavailable": 18.0,
    # First rule failure + a smaller increment per additional failure.
    "validation_failure_first": 10.0,
    "validation_failure_extra": 4.0,
    "validation_failures_cap": 22.0,
    # Soft warnings (format oddities, mock-registry miss).
    "validation_warning_each": 3.0,
    "validation_warnings_cap": 9.0,
    # --- Tampering -----------------------------------------------------------------
    # Forensic engine unavailable ⇒ document integrity unknown.
    "tampering_unavailable": 15.0,
    "tampering_inconclusive": 10.0,
    "tampering_suspicious": 18.0,
    "tampering_likely": 28.0,
    # Continuous fused forensic risk (0-100) adds a proportional share so
    # borderline evidence is not flattened into the verdict floor.
    "tampering_score_share_max": 20.0,
    # --- Face verification ----------------------------------------------------------
    # Face engine unavailable ⇒ portrait consistency unknown.
    "face_unavailable": 15.0,
    "face_inconclusive": 10.0,
    "face_no_match": 35.0,
    # Step 14 compound escalation: NO_MATCH on a document whose side is fully
    # clean (no validation failures, no tampering indicators). That is the
    # classic impostor pattern — the paper is fine, the PRESENTER is wrong —
    # and the flat face weight alone (35 → MEDIUM) undersells it for routing.
    "face_no_match_clean_document": 16.0,
    # Matched, but with soft confidence (< FACE_CONF_HIGH).
    "face_low_confidence_match": 5.0,
    # --- Document expiry --------------------------------------------------------------
    "expired": 25.0,
    "expiring_soon": 8.0,
    # Expiry date present but unreadable/implausible (not ISO).
    "expiry_unparseable": 5.0,
    # --- Missing fields ------------------------------------------------------------------
    # Per required-field gap, capped.
    "missing_field_each": 6.0,
    "missing_fields_cap": 24.0,
    # --- Suspicious field patterns -----------------------------------------------------------
    # Digit/letter confusion inside document numbers (O/0, I/1, S/5 ...).
    "suspicious_char_confusion": 6.0,
    # Whitespace, zero-width or padding characters inside a value.
    "suspicious_embedded_whitespace": 5.0,
    # Same document number appearing under different field names.
    "suspicious_duplicate_number": 8.0,
    # Name field contains digits / number field contains letters (very coarse).
    "suspicious_charset_mismatch": 7.0,
    # Cap for all suspicious-pattern points combined.
    "suspicious_patterns_cap": 18.0,
    # --- Metadata anomalies ------------------------------------------------------------------
    # Photo-editing software fingerprint in EXIF/XMP.
    "metadata_editor_fingerprint": 10.0,
    # EXIF stripped from a JPEG (weak: chat-app re-saves do this too).
    "metadata_exif_stripped": 5.0,
    # Software tag without camera make/model (weak).
    "metadata_software_without_camera": 4.0,
    # All metadata-anomaly points combined.
    "metadata_cap": 15.0,
    # --- Stage outcomes -----------------------------------------------------------------------
    # A pipeline stage crashed or was skipped ⇒ missing evidence.
    "stage_error": 10.0,
    "stage_skipped": 10.0,
    "stage_outcomes_cap": 20.0,
}

# OCR field confidence below this counts as "low confidence".
OCR_LOW_CONFIDENCE_THRESHOLD: Final[float] = 0.60

# Cap on how many OCR fields can contribute per category (prevents gaming).
OCR_FIELD_CAP: Final[int] = 8

# Face match confidence at/above this counts as a solid match.
FACE_CONF_HIGH: Final[float] = 0.70

# Registry statuses that count as serious adverse evidence.
REGISTRY_BAD_STATUSES: Final[frozenset[str]] = frozenset({"reported_lost", "reported_stolen"})

# --- Confidence in the risk verdict itself ------------------------------------------
CONF_BASE: Final[float] = 0.55          # all engines ran clean
CONF_PER_DEGRADED_STAGE: Final[float] = 0.12
CONF_DEGRADED_CAP: Final[float] = 0.45  # total deduction cap
CONF_ERROR_FLOOR: Final[float] = 0.10   # fusion itself half-blind
CONF_BAND_EDGE_PENALTY: Final[float] = 0.08  # score sits near a band boundary

# Human-review routing (additive — nothing can ever clear review).
REVIEW_SCORE_MIN: Final[int] = 25   # >= MEDIUM floor routes to review by default

# Fields treated as document-number-ish (for duplicate-value detection).
NUMBER_FIELD_HINTS: Final[frozenset[str]] = frozenset({
    "passport_number", "visa_number", "document_number", "national_id_number",
    "license_number", "permit_number", "personal_number",
})

# Pairs of visually confusable characters (case-folded) for the
# suspicious-pattern detector.
CHAR_CONFUSION_PAIRS: Final[frozenset[frozenset[str]]] = frozenset({
    frozenset({"0", "o"}), frozenset({"0", "q"}), frozenset({"1", "l"}),
    frozenset({"1", "i"}), frozenset({"5", "s"}), frozenset({"8", "b"}),
    frozenset({"2", "z"}), frozenset({"6", "g"}), frozenset({"m", "w"}),
    frozenset({"u", "v"}),
})
