"""Step 14 — SIH demonstration mode.

A controlled demonstration layer over the REAL screening pipeline for the
SIH 2026 presentation. Nothing here fakes module outputs: every demo case
uploads synthetic SPECIMEN fixtures and runs the exact production stages
(preprocess → OCR → validation → tampering → face → risk → audit).

Honesty guarantees (non-negotiable):
- Every demo response is labeled DEMONSTRATION / SIMULATED at the top level
  and carries the fixtures' synthetic-data notice.
- The face module performs a real 1:1 embedding comparison between fixtures;
  registry lookups stay MOCK-stamped; nothing simulates a government check.
- Expected-vs-actual checkpoints are evaluated from the REAL pipeline result
  and reported as-is — a checkpoint that fails is shown as failed.
- The stored/audited report keeps its canonical shape: demo markers are
  attached to the API response only, AFTER the audit hash is computed, so
  payload hashing stays verifiable.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.core.config import get_settings

DEMO_NOTICE = (
    "DEMONSTRATION / SIMULATED DATA ONLY. This result was produced in demo "
    "mode from synthetic SPECIMEN fixtures with fabricated identities. All "
    "pipeline stages ran for real; nothing here is a real identity document "
    "and nothing simulates a government verification."
)

MANIFEST_FILENAME = "manifest.json"
_RUN_ID_PREFIX = "demo_"


class DemoError(Exception):
    """Raised when a demo case cannot be located or executed."""


# ---------------------------------------------------------------------------
# Manifest / case catalog
# ---------------------------------------------------------------------------
def fixtures_dir() -> Path:
    """Directory holding the generated demo fixtures."""
    settings = get_settings()
    configured = getattr(settings, "demo_fixtures_dir", None)
    base = Path(configured) if configured else Path("data") / "demo_fixtures"
    if not base.is_absolute():
        base = Path.cwd() / base
    return base


def load_manifest() -> dict[str, Any]:
    """Load the fixture manifest (single source of truth with the generator)."""
    path = fixtures_dir() / MANIFEST_FILENAME
    if not path.is_file():
        raise DemoError(
            "Demo fixtures are not generated yet. Run: "
            "python scripts/make_demo_fixtures.py"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DemoError(f"Demo manifest is unreadable: {exc}") from exc


def list_cases() -> dict[str, Any]:
    """Manifest + the demo honesty banner (GET /api/demo/cases payload)."""
    manifest = load_manifest()
    cases = {}
    for case_id, case in manifest.get("cases", {}).items():
        cases[case_id] = {
            "title": case.get("title"),
            "storyline": case.get("storyline"),
            "expected": case.get("expected", []),
            "document": case.get("document"),
            "probe": case.get("probe"),
            "probe_alternate": case.get("probe_alternate"),
        }
    return {"notice": manifest.get("notice", DEMO_NOTICE), "cases": cases}


def require_case(case_id: str) -> tuple[str, dict[str, Any]]:
    """Return (case_id, case-def) or raise DemoError for unknown ids."""
    manifest = load_manifest()
    cases = manifest.get("cases", {})
    if case_id not in cases:
        raise DemoError(
            f"Unknown demo case '{case_id}'. Available: {sorted(cases)}", 404
        )
    return case_id, cases[case_id]


# ---------------------------------------------------------------------------
# Scripted case execution
# ---------------------------------------------------------------------------
def _fixture_path(case: dict[str, Any], key: str) -> Path:
    entry = case.get(key) or {}
    filename = entry.get("filename")
    if not filename:
        raise DemoError(f"Case fixture '{key}' is not defined in the manifest")
    path = fixtures_dir() / filename
    if not path.is_file():
        raise DemoError(
            f"Demo fixture '{filename}' is missing. Regenerate fixtures with: "
            "python scripts/make_demo_fixtures.py"
        )
    return path


def _stage_document_probe(case_id: str, case: dict[str, Any]) -> tuple[str, Path, Path, str, str]:
    """Copy the case fixtures into a fresh demo run directory.

    Returns (run_id, document_path, probe_path, doc_sha256, probe_sha256).
    The run_id carries the case id so demo runs are identifiable in history
    (e.g. ``demo_case2_tampered_ab12cd34``).
    """
    import hashlib

    settings = get_settings()
    run_id = f"{_RUN_ID_PREFIX}{case_id}_{uuid.uuid4().hex[:8]}"
    run_dir = settings.upload_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    doc_src = _fixture_path(case, "document")
    probe_src = _fixture_path(case, "probe")

    doc_dst = run_dir / f"original{doc_src.suffix.lower()}"
    probe_dst = run_dir / f"probe{probe_src.suffix.lower()}"
    shutil.copyfile(doc_src, doc_dst)
    shutil.copyfile(probe_src, probe_dst)

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    return run_id, doc_dst, probe_dst, _sha(doc_dst), _sha(probe_dst)


def run_case(session, case_id: str) -> dict[str, Any]:
    """Execute one scripted demo case through the REAL pipeline.

    Creates a Screening row, runs the orchestrator (which persists the
    consolidated report and writes the audit event), then returns the
    result wrapped in explicit DEMONSTRATION labeling with an honest
    expected-vs-actual checkpoint table.
    """
    from app.db.models import Screening
    from app.services import screening_service
    from app.services.orchestrator import Orchestrator

    case_id, case = require_case(case_id)
    run_id, doc_path, probe_path, doc_sha, probe_sha = _stage_document_probe(
        case_id, case
    )

    screening = screening_service.create_screening_row(
        session,
        run_id=run_id,
        original_path=str(doc_path),
        file_sha256=doc_sha,
        doc_type_hint="passport",
        operator_id=None,
        probe_image_path=str(probe_path),
    )
    # The orchestrator is idempotent for non-"processing" rows; fresh demo
    # rows are created in "processing" status, so this runs the pipeline.
    result = Orchestrator().run(session, screening)

    # Demo labeling is attached AFTER the orchestrator persisted the report
    # and computed the audit hash, so the stored/audited payload keeps its
    # canonical, verifiable shape (see module docstring).
    result["demo"] = {
        "is_demo": True,
        "notice": DEMO_NOTICE,
        "case_id": case_id,
        "case_title": case.get("title"),
        "fixtures": {
            "document": (case.get("document") or {}).get("filename"),
            "probe": (case.get("probe") or {}).get("filename"),
        },
    }
    result["demo"]["checks"] = evaluate_checks(case_id, result)
    result["disclaimer"] = (
        f"{DEMO_NOTICE} {result.get('disclaimer', '')}".strip()
    )
    return result


# ---------------------------------------------------------------------------
# Expected-vs-actual checkpoints (honest: computed from the REAL result)
# ---------------------------------------------------------------------------
def _risk_low_max(result: dict[str, Any]) -> float:
    bands = ((result.get("risk_assessment") or {}).get("bands")) or {}
    value = bands.get("low_max", bands.get("low"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 24.0  # settings default (risk_band_low_max)


def _ocr_ok(result: dict[str, Any]) -> tuple[bool, str]:
    ocr = result.get("ocr") or {}
    conf = ocr.get("overall_confidence")
    fields = ocr.get("fields") or []
    if isinstance(conf, (int, float)) and fields:
        return (
            True,
            f"OCR extracted {len(fields)} fields at confidence {round(conf, 3)}",
        )
    if fields:
        return True, f"OCR extracted {len(fields)} fields (no confidence score)"
    return False, "OCR produced no fields"


def _face_status(result: dict[str, Any]) -> str:
    face = result.get("face_verification") or {}
    return str(face.get("match_status") or "unknown")


def _tamper_verdict(result: dict[str, Any]) -> str:
    tampering = result.get("tampering") or {}
    return str(tampering.get("verdict") or "unknown")


def _risk_score(result: dict[str, Any]) -> float | None:
    value = (result.get("risk_assessment") or {}).get("risk_score")
    return float(value) if isinstance(value, (int, float)) else None


def _validation(result: dict[str, Any]) -> dict[str, Any]:
    validation = result.get("validation") or {}
    failures = validation.get("failures") or []
    warnings = validation.get("warnings") or []
    return {"failures": failures, "warnings": warnings}


def _check(criterion: str, met: bool, detail: str, *, informational: bool = False) -> dict[str, Any]:
    return {
        "criterion": criterion,
        "met": bool(met),
        "detail": detail,
        "informational": informational,
    }


def evaluate_checks(case_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate the scripted expectations of one case against the real result.

    Checkpoints are honest: each reports what actually happened. Items the
    spec words as "may pass" / "where appropriate" are marked informational.
    """
    checks: list[dict[str, Any]] = []
    audit_ok = (result.get("audit") or {}).get("event_id") is not None
    review = bool(result.get("human_review_required"))
    ocr_ok, ocr_detail = _ocr_ok(result)
    face = _face_status(result)
    tamper = _tamper_verdict(result)
    score = _risk_score(result)
    low_max = _risk_low_max(result)
    validation = _validation(result)

    if case_id == "case1_valid":
        checks.append(_check("OCR successful", ocr_ok, ocr_detail))
        checks.append(_check(
            "Validation passes",
            not validation["failures"],
            f"{len(validation['failures'])} failure(s), "
            f"{len(validation['warnings'])} warning(s)",
        ))
        checks.append(_check(
            "No obvious tampering",
            tamper == "no_obvious_manipulation",
            f"tampering verdict: {tamper}",
        ))
        checks.append(_check(
            "Face match", face == "MATCH", f"face verdict: {face}"
        ))
        checks.append(_check(
            "Low risk",
            score is not None and score <= low_max,
            f"risk score {score} (low band <= {low_max}); the conservative "
            f"always-review-human flag is {review} by design",
        ))
    elif case_id == "case2_tampered":
        checks.append(_check("OCR successful", ocr_ok, ocr_detail))
        checks.append(_check(
            "Validation may pass",
            True,
            f"{len(validation['failures'])} failure(s), "
            f"{len(validation['warnings'])} warning(s) — printed DOB vs MRZ "
            "mismatch is expected to surface as a warning/human cue",
            informational=True,
        ))
        checks.append(_check(
            "Tampering indicators detected",
            tamper in {"suspicious", "likely_manipulated"},
            f"tampering verdict: {tamper}",
        ))
        checks.append(_check(
            "Risk increased",
            score is not None and score > low_max,
            f"risk score {score} (low band <= {low_max})",
        ))
        checks.append(_check(
            "Human review required", review, f"review flag: {review}"
        ))
    elif case_id == "case3_identity_mismatch":
        checks.append(_check("Document information extracted", ocr_ok, ocr_detail))
        checks.append(_check(
            "Document validation passes",
            not validation["failures"],
            f"{len(validation['failures'])} failure(s), "
            f"{len(validation['warnings'])} warning(s)",
        ))
        checks.append(_check(
            "Face verification fails",
            face in {"NO_MATCH", "INCONCLUSIVE"},
            f"face verdict: {face} (expected fail: different presenter)",
        ))
        checks.append(_check(
            "High risk",
            score is not None and score > low_max,
            f"risk score {score} (low band <= {low_max})",
        ))
        checks.append(_check(
            "Human review required", review, f"review flag: {review}"
        ))
    elif case_id == "case4_low_quality":
        conf = (result.get("ocr") or {}).get("overall_confidence")
        low_conf = isinstance(conf, (int, float)) and conf < 0.6
        checks.append(_check(
            "Low OCR confidence",
            low_conf or not ocr_ok,
            f"OCR confidence {round(conf, 3) if isinstance(conf, (int, float)) else conf}"
            " (demo threshold < 0.6)",
        ))
        checks.append(_check(
            "Inconclusive validation where appropriate",
            True,
            f"{len(validation['failures'])} failure(s), "
            f"{len(validation['warnings'])} warning(s) — degraded input must "
            "degrade validation honestly, not guess",
            informational=True,
        ))
        checks.append(_check(
            "Human review required",
            review,
            f"review flag: {review} (poor quality always routes to a human)",
        ))
    else:  # pragma: no cover - guarded by require_case
        raise DemoError(f"No checkpoint rules for case '{case_id}'")

    # Every scripted case ends the same way: an audit trail record.
    checks.append(_check(
        "Audit recorded",
        audit_ok,
        "audit event recorded" if audit_ok else "audit event MISSING",
    ))
    return checks


def checks_summary(checks: list[dict[str, Any]]) -> dict[str, int]:
    """Small rollup for UI badges (informational checks never count as met)."""
    hard = [c for c in checks if not c.get("informational")]
    return {
        "total": len(checks),
        "met": sum(1 for c in hard if c["met"]),
        "failed": sum(1 for c in hard if not c["met"]),
        "informational": len(checks) - len(hard),
    }
