"""Step 14 — Demonstration-mode REST endpoints (SIH 2026 presentation).

    GET  /api/demo/cases            → case catalog + synthetic-data notice
    POST /api/demo/run/{case_id}    → run one scripted case end-to-end

Every response carries an explicit DEMONSTRATION / SIMULATED label. The run
endpoint executes the REAL pipeline (all stages) on synthetic SPECIMEN
fixtures — it never fabricates module outputs and never fakes a government
verification. Registry lookups stay MOCK-stamped exactly as in production.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.responses import DISCLAIMER, ok
from app.core.security import require_screening_run
from app.db.base import get_db
from app.services import demo_mode

router = APIRouter(prefix="/api/demo", tags=["demo"])

DbDep = Depends(get_db)
RunGuard = Depends(require_screening_run)


@router.get("/cases")
def demo_cases(_user=RunGuard):
    """The scripted case catalog (fixtures, storylines, expected outcomes)."""
    try:
        payload = demo_mode.list_cases()
    except demo_mode.DemoError as exc:
        raise AppError(str(exc), status_code=503) from exc
    return ok(payload)


@router.post("/run/{case_id}")
def demo_run(
    case_id: str,
    background: BackgroundTasks,
    db: Session = DbDep,
    _user=RunGuard,
):
    """Run one scripted demo case through the complete real pipeline."""
    try:
        case_id, case = demo_mode.require_case(case_id)
    except demo_mode.DemoError as exc:
        status = 404 if "Unknown demo case" in str(exc) else 503
        raise AppError(str(exc), status_code=status) from exc

    try:
        result = demo_mode.run_case(db, case_id, operator_id=_user.id)
    except demo_mode.DemoError as exc:
        raise AppError(str(exc), status_code=503) from exc

    db.commit()  # durability before the response returns (parity with analyze)

    # Background chain anchor for the fresh audit event, same as production.
    event_id = (result.get("audit") or {}).get("event_id")
    if event_id is not None:
        from app.services.audit.service import anchor_event

        background.add_task(anchor_event, event_id)

    return ok(
        {
            "run_id": result.get("screening_id"),
            "case_id": case_id,
            "case_title": case.get("title"),
            "demo_notice": demo_mode.DEMO_NOTICE,
            "disclaimer": DISCLAIMER,
            "result": result,
        }
    )
