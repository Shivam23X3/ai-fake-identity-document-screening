"""Human review endpoints — Step 15 completion of the human-in-the-loop loop.

    GET  /api/review/pending          → queue of undecided screenings
    GET  /api/review/{run_id}         → decision state for one screening
    POST /api/review/{run_id}/decide  → record the human decision (RBAC'd)

Every decision is attributed to the authenticated reviewer, persisted in the
``reviews`` table and mirrored into the audit trail (hash-only). The AI
pipeline never records a decision: only a REVIEWER/ADMIN can close a case.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.responses import DISCLAIMER, ok
from app.core.security import require_permission
from app.db.base import get_db
from app.services import review_service, screening_service

router = APIRouter(prefix="/api/review", tags=["review"])

DbDep = Annotated[Session, Depends(get_db)]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(min_length=1, max_length=32)
    notes: str | None = Field(default=None, max_length=2000)


@router.get("/pending")
def list_pending(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    db: DbDep = None,
    _user=Depends(require_permission("screening:read")),
):
    """Screenings awaiting a human decision (oldest first)."""
    rows = review_service.pending_reviews(db, limit=limit)
    return ok(
        {
            "items": [
                {
                    "run_id": r.run_id,
                    "status": r.status,
                    "risk_score": r.risk_score,
                    "risk_band": r.risk_band,
                    "human_review_required": r.human_review_required,
                    "doc_type_detected": r.doc_type_detected,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
            "count": len(rows),
            "disclaimer": DISCLAIMER,
        }
    )


@router.get("/{run_id}")
def get_review(run_id: str, db: DbDep = None, _user=Depends(require_permission("screening:read"))):
    screening = screening_service.require_screening(db, run_id)
    return ok(
        {
            "run_id": screening.run_id,
            "human_review_required": screening.human_review_required,
            "review": review_service.review_state(db, screening),
            "disclaimer": DISCLAIMER,
        }
    )


@router.post("/{run_id}/decide")
def decide(
    run_id: str,
    body: DecisionRequest,
    db: DbDep = None,
    user=Depends(require_permission("screening:read")),
):
    """Record the human decision for one screening (REVIEWER and above)."""
    screening = screening_service.require_screening(db, run_id)
    result = review_service.record_decision(
        db,
        screening=screening,
        reviewer_id=user.id,
        decision=body.decision,
        notes=body.notes,
    )
    return ok(
        {
            "run_id": screening.run_id,
            "decision": result["review"].decision,
            "decided_at": result["review"].decided_at.isoformat(),
            "reviewer_id": user.id,
            "audit_event_id": result["audit_event"].id,
            "disclaimer": DISCLAIMER,
        },
        status_code=201,
    )


__all__ = ["router"]
