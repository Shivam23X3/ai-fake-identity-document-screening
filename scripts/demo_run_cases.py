"""Step 14 — CLI rehearsal for the SIH demonstration mode.

Runs all four scripted demo cases end-to-end through the REAL pipeline and
prints the honest expected-vs-actual checkpoint table. Mirrors
POST /api/demo/run/{case_id} for presenters who prefer the terminal.

    .venv/Scripts/python scripts/demo_run_cases.py             # all cases
    .venv/Scripts/python scripts/demo_run_cases.py case2_tampered

Everything produced here is DEMONSTRATION / SIMULATED output on synthetic
SPECIMEN fixtures. No government verification is simulated anywhere: the
pipeline stages are the real implementations and registry lookups stay
MOCK-stamped.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CASES = (
    "case1_valid",
    "case2_tampered",
    "case3_identity_mismatch",
    "case4_low_quality",
)


def _print_checks(result: dict) -> None:
    # ASCII marks only: Windows consoles default to cp1252 and crash on ✓/✗.
    for check in result.get("demo", {}).get("checks", []):
        mark = "OK  " if check["met"] else "FAIL"
        info = " (informational)" if check.get("informational") else ""
        print(f"    [{mark}] {check['criterion']}{info}")
        print(f"        {check['detail']}")


def run_one(case_id: str) -> int:
    from app.db.base import create_all, get_session_factory
    from app.services import demo_mode

    create_all()
    session = get_session_factory()()
    try:
        result = demo_mode.run_case(session, case_id)
        session.commit()
    finally:
        session.close()

    face = result.get("face_verification") or {}
    risk = result.get("risk_assessment") or {}
    tampering = result.get("tampering") or {}
    ocr = result.get("ocr") or {}

    print(f"\n=== {case_id} — {result.get('demo', {}).get('case_title', '')}")
    print(f"  run_id   : {result.get('screening_id')}  (DEMONSTRATION/SIMULATED)")
    print(
        f"  OCR      : conf={ocr.get('overall_confidence')} "
        f"fields={len(ocr.get('fields') or {})} mrz={'yes' if ocr.get('mrz_raw') else 'no'}"
    )
    print(f"  Validation: failures={len((result.get('validation') or {}).get('failures') or [])}")
    print(f"  Tampering : {tampering.get('verdict')}")
    print(
        f"  Face      : {face.get('match_status')} "
        f"(similarity={face.get('similarity_score')})"
    )
    print(
        f"  Risk      : {risk.get('risk_level')} ({risk.get('risk_score')}/100) "
        f"review={result.get('human_review_required')}"
    )
    audit = result.get("audit") or {}
    print(f"  Audit     : event={audit.get('event_id')} sha256={str(audit.get('payload_sha256'))[:16]}…")
    _print_checks(result)

    hard_failed = [
        c for c in result.get("demo", {}).get("checks", [])
        if not c["met"] and not c.get("informational")
    ]
    return 1 if hard_failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SIH scripted demo cases")
    parser.add_argument("case", nargs="?", choices=CASES, help="one case, or all when omitted")
    args = parser.parse_args()

    print("DEMONSTRATION MODE - synthetic fixtures, real pipeline, no government verification.")
    cases = [args.case] if args.case else list(CASES)
    failures = 0
    for case_id in cases:
        failures += run_one(case_id)

    print("\nSummary:", "ALL CHECKPOINTS MET" if failures == 0 else f"{failures} case(s) with unmet hard checkpoints")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
