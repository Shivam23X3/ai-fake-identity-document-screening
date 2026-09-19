"""Audit service: the local, chain-anchored trail (Step 9).

For every completed screening:
1. compute ``sha256(canonical_json(report))``,
2. write an ``audit_events`` row (event_type, payload hash, anchor state),
3. attempt the on-chain anchor via :class:`AuditAnchorClient` (BackgroundTask
   on the API path so the HTTP response never waits for a block confirmation).

Hash-only on chain; the full report stays in the local DB. When the chain
is unreachable the anchor honestly stays ``pending`` — nothing is faked.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import AuditEvent
from app.services.audit.chain_client import AuditAnchorClient
from app.services.audit.hashing import sha256_hex

logger = logging.getLogger(__name__)

_client: AuditAnchorClient | None = None


def get_chain_client() -> AuditAnchorClient:
    """Process-wide lazy chain client (probes the RPC once)."""
    global _client
    if _client is None:
        _client = AuditAnchorClient()
    return _client


def record_screening_completed(
    session: Session,
    *,
    screening_id: int,
    run_id: str,
    report: dict[str, Any],
) -> AuditEvent:
    """Write the audit event row for a completed screening (unanchored yet)."""
    payload_sha = sha256_hex(report)
    event = AuditEvent(
        screening_id=screening_id,
        event_type="screening_completed",
        payload_sha256=payload_sha,
        anchor_status="pending",
    )
    session.add(event)
    session.flush()
    return event


def anchor_event(event_id: int) -> dict[str, Any]:
    """Attempt the on-chain anchor for one audit event (runs in background).

    Updates the row in place with tx_hash / block_number / anchor_status.
    Safe to call from a BackgroundTask: opens its own DB session and never
    raises.
    """
    from app.db.base import get_session_factory
    from sqlalchemy import select

    factory = get_session_factory()
    session = factory()
    try:
        event = session.execute(
            select(AuditEvent).where(AuditEvent.id == event_id)
        ).scalar_one_or_none()
        if event is None:
            return {"status": "failed", "error": "audit event not found"}
        if event.anchor_status == "anchored":
            return {"status": "anchored", "note": "already anchored"}

        # Resolve the screening's run_id so the on-chain key is the same one
        # verification uses: (int(run_id[:8], 16), reportHash).
        from app.db.models import Screening

        screening = session.execute(
            select(Screening).where(Screening.id == event.screening_id)
        ).scalar_one_or_none()
        if screening is None:
            return {"status": "failed", "error": "screening row not found"}

        result = get_chain_client().anchor(screening.run_id, event.payload_sha256)
        # Contract stores (numericId(run_id), reportHash, reviewerHash=reportHash
        # until a human decision exists, linkage).
        event.chain = "hardhat-local" if result.get("status") == "anchored" else event.chain
        event.tx_hash = result.get("tx_hash")
        event.block_number = result.get("block_number")
        if result.get("status") == "anchored":
            event.anchor_status = "anchored"
        elif result.get("status") == "failed":
            event.anchor_status = "failed"
        session.commit()
        return dict(result)
    except Exception as exc:  # noqa: BLE001 - background task must never raise
        logger.warning("anchor_event(%s) failed: %s", event_id, exc)
        session.rollback()
        return {"status": "failed", "error": str(exc)[:300]}
    finally:
        session.close()


def verify_screening(screening_run_id: str, report: dict[str, Any]) -> dict[str, Any]:
    """Re-hash the stored report and check it against the on-chain anchor."""
    expected = sha256_hex(report)
    result = get_chain_client().verify(screening_run_id, expected)
    return {
        "run_id": screening_run_id,
        "report_hash": expected,
        "canonical_note": "sha256 of canonical JSON (sorted keys, compact separators)",
        **result,
    }


def chain_status() -> dict[str, Any]:
    """Diagnostics for /pipeline/info and the dashboard footer."""
    return get_chain_client().status()


# ---------------------------------------------------------------------------
# Step 10 — canonical audit records on the permissioned ledger
# ---------------------------------------------------------------------------
def _require_analyzed_screening(session: Session, screening) -> dict[str, Any]:
    """Load the stored Step-9 report (decrypted), requiring an orchestrator run."""
    from app.services import screening_service

    report = screening_service.load_report(screening)
    if not isinstance(report, dict) or "final_status" not in report:
        raise AppError(
            "This screening has not been analyzed yet. POST /api/screening/analyze "
            "first — the audit record is derived from the consolidated result.",
            409,
        )
    return report


def log_screening_to_ledger(session: Session, screening) -> dict[str, Any]:
    """Build the canonical audit record, store its metadata on the ledger
    and keep a local DB copy (audit_events, type 'audit_logged').

    Hash-only on the ledger: images, biometrics and personal fields never
    leave the local database.
    """
    from app.services.audit.ledger import build_audit_record, get_ledger_backend

    report = _require_analyzed_screening(session, screening)
    record = build_audit_record(report)

    backend_name, backend = get_ledger_backend()
    ledger_result = backend.append(record)

    # Local DB copy of the record (the durable trail; the local mock ledger
    # is process-bound, the EVM ledger stores the hash only).
    import json as _json

    event = AuditEvent(
        screening_id=screening.id,
        event_type="audit_logged",
        payload_sha256=record["result_hash"],
        chain=f"{backend_name}-ledger",
        tx_hash=ledger_result.get("tx_hash"),
        block_number=ledger_result.get("block_number"),
        anchor_status=ledger_result.get("ledger_status", "failed"),
    )
    session.add(event)
    session.flush()

    return {
        "audit_record": record,
        "ledger_backend": backend_name,
        "ledger": {
            k: v for k, v in ledger_result.items() if k not in {"result_hash"}
        },
        "event_id": event.id,
        "record_json": _json.dumps(record, default=str),
    }


def get_audit_record(session: Session, screening) -> dict[str, Any]:
    """Return the canonical audit record + ledger state for one screening."""
    from sqlalchemy import select

    from app.services.audit.ledger import build_audit_record, get_ledger_backend

    report = _require_analyzed_screening(session, screening)
    record = build_audit_record(report)  # deterministic fields except timestamp

    events = list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.screening_id == screening.id)
            .order_by(AuditEvent.created_at.asc())
        ).scalars()
    )
    logged = [e for e in events if e.event_type == "audit_logged"]
    anchored = [e for e in events if e.event_type == "screening_completed"]

    backend_name, backend = get_ledger_backend()
    ledger_entry = backend.get(screening.run_id) if hasattr(backend, "get") else None

    return {
        "screening_id": screening.run_id,
        "audit_record": record,
        "ledger_backend": backend_name,
        "ledger_entry": ledger_entry,
        "events": [
            {
                "event_type": e.event_type,
                "payload_sha256": e.payload_sha256,
                "anchor_status": e.anchor_status,
                "chain": e.chain,
                "tx_hash": e.tx_hash,
                "block_number": e.block_number,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
        "has_audit_log": bool(logged),
        "screening_anchor": (
            {
                "payload_sha256": anchored[-1].payload_sha256,
                "anchor_status": anchored[-1].anchor_status,
                "tx_hash": anchored[-1].tx_hash,
            }
            if anchored
            else None
        ),
    }


def verify_audit_record(session: Session, screening) -> dict[str, Any]:
    """Recompute the result hash from the STORED report and compare it with
    the ledger commitment — detects any change to the screening result."""
    from sqlalchemy import select

    from app.services.audit.ledger import get_ledger_backend

    from app.services.audit.ledger import build_audit_record

    report = _require_analyzed_screening(session, screening)
    record = build_audit_record(report)
    recomputed_hash = record["result_hash"]

    backend_name, backend = get_ledger_backend()
    verify = backend.verify(screening.run_id, recomputed_hash)

    # Local DB trail: the committed hash at analyze time (never moved).
    event = session.execute(
        select(AuditEvent)
        .where(AuditEvent.screening_id == screening.id)
        .where(AuditEvent.event_type == "screening_completed")
        .order_by(AuditEvent.created_at.desc())
    ).scalars().first()
    db_committed = event.payload_sha256 if event else None

    db_match = db_committed == recomputed_hash if db_committed else None
    verified = bool(verify.get("verified"))
    changed = db_match is False or (
        verify.get("on_ledger") and not verified
    )

    return {
        "screening_id": screening.run_id,
        "recomputed_result_hash": recomputed_hash,
        "db_committed_hash": db_committed,
        "db_hash_match": db_match,
        "ledger_backend": backend_name,
        "ledger_verify": verify,
        "verified": verified,
        "on_ledger": bool(verify.get("on_ledger")),
        "record_changed": changed,
        "conclusion": (
            "UNCHANGED — recomputed hash matches the ledger commitment"
            if verified
            else (
                "CHANGED — the screening result no longer matches the ledger "
                "commitment; investigate immediately"
                if changed
                else (
                    "NOT ON LEDGER — no commitment exists for this screening "
                    "on the selected backend (log it first)"
                    if verify.get("reason") == "no_ledger_entry"
                    else "UNAVAILABLE — ledger unreachable; verification inconclusive"
                )
            )
        ),
    }
