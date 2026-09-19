"""Step-10 audit endpoints: canonical audit records on the ledger.

    POST /api/audit/log                  → build + log the canonical audit record
    GET  /api/audit/{screening_id}       → audit record + ledger state
    GET  /api/audit/verify/{screening_id} → recompute hash, detect tampering

Design (per the Step-10 spec): the ledger receives HASHES + statuses only —
document images, biometric data and personal fields never leave the local
database. The ledger backend is pluggable (local mock / EVM / future Fabric)
via ``APP_LEDGER_BACKEND``; these endpoints are backend-agnostic.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.responses import DISCLAIMER, ok
from app.core.security import require_admin, require_audit_read, require_audit_write
from app.db.base import get_db
from app.services import screening_service
from app.services.audit import service as audit_service

router = APIRouter(prefix="/api/audit", tags=["audit"])

DbDep = Annotated[Session, Depends(get_db)]


@router.post("/log")
def log_audit(body: dict, db: DbDep = None, _user=Depends(require_audit_write)):
    """Build the canonical audit record for one screening and log it on the
    selected permissioned ledger backend.

    Body: ``{"run_id": "<screening run id>"}``. The record carries only
    screening_id, timestamp, result hash, risk score, validation /
    tampering / face-verification statuses and the system identifier.
    """
    run_id = str((body or {}).get("run_id") or "").strip()
    if not run_id:
        return ok(
            {
                "logged": False,
                "error": "Body must be {\"run_id\": \"...\"} — the screening to log.",
                "disclaimer": DISCLAIMER,
            },
            status_code=422,
        )

    screening = screening_service.require_screening(db, run_id)
    result = audit_service.log_screening_to_ledger(db, screening)
    return ok(
        {
            "logged": result["ledger"].get("ledger_status") not in {"failed", None},
            "screening_id": screening.run_id,
            "audit_record": result["audit_record"],
            "ledger_backend": result["ledger_backend"],
            "ledger": result["ledger"],
            "event_id": result["event_id"],
            "note": (
                "Hash-only on the ledger: document images, biometric data and "
                "personal fields never leave the local database."
            ),
            "disclaimer": DISCLAIMER,
        }
    )


@router.get("/status")
def audit_status(_user=Depends(require_audit_read)):
    """Ledger backend diagnostics (which backend is active and its state)."""
    from app.services.audit.ledger import ledger_status

    return ok({"ledger": ledger_status(), "disclaimer": DISCLAIMER})


@router.get("/verify/{screening_id}")
def verify_audit(
    screening_id: str, db: DbDep = None, _user=Depends(require_audit_read)
):
    """Recompute the result hash from the STORED report and compare it with
    the ledger commitment + the local DB trail.

    Reports one of: verified (UNCHANGED) / record_changed (CHANGED) /
    not on ledger / ledger unavailable — honest in every direction.
    """
    screening = screening_service.require_screening(db, screening_id)
    result = audit_service.verify_audit_record(db, screening)
    return ok({**result, "disclaimer": DISCLAIMER})


@router.get("/{screening_id}")
def get_audit(
    screening_id: str, db: DbDep = None, _user=Depends(require_audit_read)
):
    """The canonical audit record + ledger state + full local event trail."""
    screening = screening_service.require_screening(db, screening_id)
    result = audit_service.get_audit_record(db, screening)
    return ok({**result, "disclaimer": DISCLAIMER})
