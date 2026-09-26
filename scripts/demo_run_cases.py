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

Step 15 addition: ``--with-review`` closes the human-in-the-loop on every
run by recording a plausible reviewer decision (cleared/flagged/escalated/
inconclusive per case) so presenters can rehearse the new Human Review tab
without a browser. "HUMAN" is printed next to any operator-attributed step.
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

# Plausible human decisions per demo case (rehearsal script, not pipeline
# behavior): what a trained reviewer would most likely record.
CASE_REVIEW_DECISIONS = {
    "case1_valid": "cleared",
    "case2_tampered": "flagged",
    "case3_identity_mismatch": "escalated",
    "case4_low_quality": "inconclusive",
}


def _print_checks(result: dict) -> None:
    # ASCII marks only: Windows consoles default to cp1252 and crash on ✓/✗.
    for check in result.get("demo", {}).get("checks", []):
        mark = "OK  " if check["met"] else "FAIL"
        info = " (informational)" if check.get("informational") else ""
        print(f"    [{mark}] {check['criterion']}{info}")
        print(f"        {check['detail']}")


def _record_rehearsal_review(case_id: str, screening_id: str) -> None:
    """Close the HITL loop for one demo run (REHEARSAL ONLY).

    The review is attributed to reviewer_id=None-equivalent (the service
    requires an integer, so we create/reuse a dedicated 'demo-reviewer'
    account). This mirrors POST /api/review/{run_id}/decide exactly — same
    service, same audit event — so the CLI rehearsal exercises the same
    code path the UI button hits.
    """
    from app.db.base import get_session_factory
    from app.db.models import User
    from app.services import review_service, screening_service
    from app.services.user_service import hash_password

    factory = get_session_factory()
    session = factory()
    try:
        from sqlalchemy import select

        reviewer = session.execute(
            select(User).where(User.username == "demo-reviewer")
        ).scalar_one_or_none()
        if reviewer is None:
            reviewer = User(
                username="demo-reviewer",
                password_hash=hash_password("Demo#Reviewer#Rehearsal#1"),
                role="REVIEWER",
                is_active=True,
            )
            session.add(reviewer)
            session.flush()
        screening = screening_service.require_screening(session, screening_id)
        decision = CASE_REVIEW_DECISIONS.get(case_id, "inconclusive")
        review_service.record_decision(
            session,
            screening=screening,
            reviewer_id=reviewer.id,
            decision=decision,
            notes=f"CLI rehearsal decision for {case_id}",
        )
        session.commit()
        print(
            f"  HUMAN     : {decision} by 'demo-reviewer' (review_decided audit event written)"
        )
    finally:
        session.close()


def run_one(case_id: str, with_review: bool = False) -> int:
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
    if with_review:
        _record_rehearsal_review(case_id, str(result.get("screening_id")))
    _print_checks(result)

    hard_failed = [
        c for c in result.get("demo", {}).get("checks", [])
        if not c["met"] and not c.get("informational")
    ]
    return 1 if hard_failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SIH scripted demo cases")
    parser.add_argument("case", nargs="?", choices=CASES, help="one case, or all when omitted")
    parser.add_argument(
        "--with-review",
        action="store_true",
        help="record the plausible human decision per case (closes the HITL loop; rehearsal only)",
    )
    args = parser.parse_args()

    print("DEMONSTRATION MODE - synthetic fixtures, real pipeline, no government verification.")
    cases = [args.case] if args.case else list(CASES)
    failures = 0
    for case_id in cases:
        failures += run_one(case_id, with_review=args.with_review)

    print("\nSummary:", "ALL CHECKPOINTS MET" if failures == 0 else f"{failures} case(s) with unmet hard checkpoints")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
