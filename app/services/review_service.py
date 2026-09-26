"""Human review workflow — the closing half of the human-in-the-loop design.

The pipeline flags cases and forces ``human_review_required``; this service
is where a REVIEWER/ADMIN records the human decision that the AI is never
allowed to make. Every decision is persisted (``reviews`` table, one active
decision per screening), attributed to the authenticated reviewer, and
mirrored into the audit trail as a ``review_decided`` event (hash-only, like
every audit event).

Vocabulary (kept deliberately small and explicit):
- ``cleared``   — the human examined the case and found it acceptable.
- ``flagged``   — the human confirmed a problem (fraud suspicion, quality).
- ``escalated`` — the human routed the case to a higher authority.
- ``inconclusive`` — the human could not decide; more evidence needed.

The service never re-runs the pipeline and never changes module verdicts:
a decision is purely an overlay that downstream consumers (history,
investigation, audit) can join onto the AI output.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import Review, Screening

logger = logging.getLogger(__name__)

DECISIONS = ("cleared", "flagged", "escalated", "inconclusive")


def get_latest_review(session: Session, screening_id: int) -> Review | None:
    """The most recent decision for one screening (None = undecided)."""
    return session.execute(
        select(Review)
        .where(Review.screening_id == screening_id)
        .order_by(Review.decided_at.desc(), Review.id.desc())
    ).scalars().first()


def review_state(session: Session, screening: Screening) -> dict[str, Any]:
    """Decision block for one screening (API shape; None-safe)."""
    review = get_latest_review(session, screening.id)
    if review is None:
        return {
            "decision": None,
            "notes": None,
            "reviewer_id": None,
            "decided_at": None,
            "pending": True,
        }
    return {
        "decision": review.decision,
        "notes": review.notes,
        "reviewer_id": review.reviewer_id,
        "decided_at": review.decided_at.isoformat() if review.decided_at else None,
        "pending": False,
    }


def record_decision(
    session: Session,
    *,
    screening: Screening,
    reviewer_id: int,
    decision: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Persist the human decision + mirror it into the audit trail.

    - ``decision`` must be one of :data:`DECISIONS` (422 otherwise).
    - The screening row is re-opened from ``pending_review`` to ``reviewed``
      so the history shows the human closed the loop.
    - A ``review_decided`` audit event commits ``sha256`` of a small,
      PII-free payload: run_id, decision, reviewer id, decision notes hash.
    """
    if decision not in DECISIONS:
        raise AppError(
            f"decision must be one of {', '.join(DECISIONS)}", 422
        )

    review = Review(
        screening_id=screening.id,
        reviewer_id=reviewer_id,
        decision=decision,
        notes=notes,
    )
    session.add(review)
    screening.status = "reviewed"
    session.flush()

    # --- audit-trail mirror (hash-only, PII-free) --------------------------
    from app.db.models import AuditEvent
    from app.services.audit.hashing import sha256_hex

    payload = {
        "event_type": "review_decided",
        "run_id": screening.run_id,
        "decision": decision,
        "reviewer_id": reviewer_id,
        "notes_present": bool(notes),
    }
    event = AuditEvent(
        screening_id=screening.id,
        event_type="review_decided",
        payload_sha256=sha256_hex(payload),
        anchor_status="pending",
    )
    session.add(event)
    session.flush()

    logger.info(
        "Review decision recorded: screening_id=%s decision=%s reviewer_id=%s",
        screening.id,
        decision,
        reviewer_id,
    )
    return {"review": review, "audit_event": event}


def pending_reviews(session: Session, *, limit: int = 50) -> list[Screening]:
    """Screenings awaiting a human decision (newest first).

    Newest-first matches the dashboard's history ordering and keeps the
    most recently flagged case at the top of a live demo queue; a deployment
    preferring SLA-fairness (oldest first) can flip the order_by.
    """
    decided_ids = select(Review.screening_id).scalar_subquery()
    return list(
        session.execute(
            select(Screening)
            .where(Screening.id.not_in(decided_ids))
            .order_by(Screening.created_at.desc(), Screening.id.desc())
            .limit(limit)
        ).scalars()
    )


__all__ = [
    "DECISIONS",
    "get_latest_review",
    "pending_reviews",
    "record_decision",
    "review_state",
]
