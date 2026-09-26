"""Screening REST endpoints (Step 2 contract + Step 3 file preview).

    POST /api/screening/upload      → validate + store file, create run
    POST /api/screening/analyze     → run the pipeline for an uploaded run
    GET  /api/screening/{run_id}    → full report for one run
    GET  /api/screening/{run_id}/file → serve the stored document image
    GET  /api/screening/history     → paginated list of runs

Advanced AI is intentionally NOT here: stages call pluggable providers
(app/services/ai_providers.py). Until real engines are registered, the
analyze endpoints return honest not_implemented results.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.pipeline import PipelineContext
from app.core.rate_limit import limiter
from app.core.responses import DISCLAIMER, err, ok
from app.core.security import (
    require_audit_read,
    require_screening_read,
    require_screening_run,
)
from app.db.base import get_db
from app.pipelines import build_document_screening_pipeline
from app.schemas.screening import AnalyzeRequest
from app.services import screening_service


def _rmtree_quiet(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)

router = APIRouter(prefix="/api/screening", tags=["screening"])

DbDep = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# POST /api/screening/upload
# ---------------------------------------------------------------------------
@router.post("/upload")
@limiter.limit(lambda: get_settings().rate_limit_upload)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    probe_image: Annotated[UploadFile | None, Form()] = None,
    doc_type_hint: Annotated[str, Form()] = "unknown",
    db: DbDep = None,
    _user=Depends(require_screening_run),
):
    if doc_type_hint not in {"passport", "visa", "national_id", "driving_license", "permit", "unknown"}:
        return err(
            "doc_type_hint must be one of passport, visa, national_id, "
            "driving_license, permit, unknown",
            422,
        )

    async def _chunks():
        while chunk := await file.read(1024 * 1024):
            yield chunk

    saved = await screening_service.save_upload(file.filename or "upload", _chunks())

    # Optional live/presented-person image for 1:1 face verification (Step 7).
    probe_saved: dict | None = None
    if probe_image is not None and (probe_image.filename or "").strip():

        async def _probe_chunks():
            while chunk := await probe_image.read(1024 * 1024):
                yield chunk

        try:
            probe_saved = await screening_service.save_upload(
                probe_image.filename or "probe", _probe_chunks()
            )
        except AppError:
            _rmtree_quiet(Path(saved["path"]).parent)
            raise

    # Attribution: the JWT-authenticated operator who created this run
    # (audit trail value — Step 11 added auth, this wires it through).
    operator = request.state.user
    screening_service.create_screening_row(
        db,
        run_id=saved["run_id"],
        original_path=str(saved["path"]),
        file_sha256=saved["sha256"],
        doc_type_hint=doc_type_hint,
        operator_id=operator.id,
        probe_image_path=str(probe_saved["path"]) if probe_saved else None,
        pdf_pages=saved.get("pdf_pages"),
    )

    return ok(
        {
            "run_id": saved["run_id"],
            "status": "uploaded",
            "doc_type_hint": doc_type_hint,
            "file": {
                "filename": file.filename,
                "size_bytes": saved["size"],
                "sha256": saved["sha256"],
                "detected_type": saved["detected_type"],
                **({"pdf_pages": saved["pdf_pages"]} if saved.get("pdf_pages") else {}),
            },
            "probe_image": (
                {
                    "filename": probe_image.filename,
                    "size_bytes": probe_saved["size"],
                    "sha256": probe_saved["sha256"],
                }
                if probe_saved
                else None
            ),
            "next_step": "POST /api/screening/analyze with this run_id",
            "disclaimer": DISCLAIMER,
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# POST /api/screening/analyze
# ---------------------------------------------------------------------------
@router.post("/analyze")
@limiter.limit(lambda: get_settings().rate_limit_analyze)
def analyze_screening(
    request: Request,
    body: AnalyzeRequest,
    background: BackgroundTasks,
    db: DbDep = None,
    _user=Depends(require_screening_run),
):
    """Run the complete screening workflow for one uploaded run.

    The Step-9 orchestrator coordinates every module (preprocess → OCR →
    validation → tampering → face → risk), persists the consolidated
    result and writes the audit trail. The on-chain anchor attempt runs as
    a BackgroundTask so the HTTP response never waits for a block.
    """
    screening = screening_service.require_screening(db, body.run_id)
    from app.services.orchestrator import Orchestrator

    result = Orchestrator().run(db, screening)

    # The background anchor task opens its OWN DB session, so everything it
    # needs (the audit_events row, the consolidated report) must be durable
    # BEFORE the task runs — the request-scoped session's teardown commit
    # happens too late to be visible to it.
    db.commit()

    # Background chain anchor (hashes only) when an audit event was created.
    event_id = (result.get("audit") or {}).get("event_id")
    if event_id is not None:
        from app.services.audit.service import anchor_event

        background.add_task(anchor_event, event_id)

    return ok(
        {
            # legacy flat keys (existing dashboard/history consumers)
            "run_id": screening.run_id,
            "status": screening.status,
            "doc_type_hint": screening.doc_type_hint,
            "doc_type_detected": screening.doc_type_detected,
            "risk_score": screening.risk_score,
            "risk_band": screening.risk_band,
            "human_review_required": screening.human_review_required,
            "stages": result.get("stages", []),
            # Step-9 consolidated payload
            "result": result,
            "disclaimer": DISCLAIMER,
        }
    )


# ---------------------------------------------------------------------------
# GET /api/screening/history   (MUST be declared before /{run_id})
# ---------------------------------------------------------------------------
@router.get("/history")
def screening_history(
    db: DbDep = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    _user=Depends(require_screening_read),
):
    rows = screening_service.list_history(db, limit=limit, offset=offset)
    items = [
        {
            "run_id": r.run_id,
            "doc_type_hint": r.doc_type_hint,
            "doc_type_detected": r.doc_type_detected,
            "status": r.status,
            "risk_score": r.risk_score,
            "risk_band": r.risk_band,
            "human_review_required": r.human_review_required,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return ok({"items": items, "limit": limit, "offset": offset, "count": len(items)})


# ---------------------------------------------------------------------------
# GET /api/screening/{run_id}
# ---------------------------------------------------------------------------
@router.get("/{run_id}")
def get_screening(
    run_id: str, db: DbDep = None, _user=Depends(require_screening_read)
):
    screening = screening_service.require_screening(db, run_id)
    report = screening_service.load_report(screening)
    if not report:
        report = {"raw": "<unreadable report>"}
    return ok(
        {
            "run_id": screening.run_id,
            "status": screening.status,
            "doc_type_hint": screening.doc_type_hint,
            "doc_type_detected": screening.doc_type_detected,
            "risk_score": screening.risk_score,
            "risk_band": screening.risk_band,
            "human_review_required": screening.human_review_required,
            "file_sha256": screening.file_sha256,
            "created_at": screening.created_at.isoformat() if screening.created_at else None,
            "completed_at": screening.completed_at.isoformat() if screening.completed_at else None,
            "report": report,
            "disclaimer": DISCLAIMER,
        }
    )


# ---------------------------------------------------------------------------
# GET /api/screening/{run_id}/result — Step-9 consolidated result
# ---------------------------------------------------------------------------
@router.get("/{run_id}/result")
def get_screening_result(
    run_id: str, db: DbDep = None, _user=Depends(require_screening_read)
):
    """The complete Step-9 screening result in the spec's consolidated shape:
    screening_id, document, ocr, validation, tampering, face_verification,
    risk_assessment, final_status, human_review_required, processing_time_ms
    — plus the audit-trail block (report hash + anchor state).
    """
    screening = screening_service.require_screening(db, run_id)
    report = screening_service.load_report(screening)
    if "final_status" not in report:
        raise AppError(
            "This run has not been analyzed by the Step-9 orchestrator yet. "
            "POST /api/screening/analyze first.",
            409,
        )
    from sqlalchemy import select

    from app.db.models import AuditEvent
    from app.services.audit.service import verify_screening

    # Prefer the Step-9 screening_completed event; multiple events per
    # screening exist once Step-10 audit-log records are added.
    event = (
        db.execute(
            select(AuditEvent)
            .where(AuditEvent.screening_id == screening.id)
            .order_by(
                (AuditEvent.event_type == "screening_completed").desc(),
                AuditEvent.created_at.desc(),
            )
        )
        .scalars()
        .first()
    )
    audit_block = {
        "event_id": event.id if event else None,
        "payload_sha256": event.payload_sha256 if event else None,
        "anchor_status": event.anchor_status if event else None,
        "tx_hash": event.tx_hash if event else None,
        "block_number": event.block_number if event else None,
    }
    if event:
        try:
            # verify_screening re-hashes the stored report DICT and compares
            # it with the on-chain commitment keyed by (run_id, hash).
            audit_block["on_chain"] = verify_screening(run_id, report)
        except Exception as exc:  # noqa: BLE001 - verification is advisory
            audit_block["on_chain"] = {"verified": False, "reason": str(exc)[:200]}

    return ok({**report, "audit": audit_block})


# ---------------------------------------------------------------------------
# GET /api/screening/{run_id}/audit — audit trail + chain anchor status
# ---------------------------------------------------------------------------
@router.get("/{run_id}/audit")
def get_screening_audit(
    run_id: str, db: DbDep = None, _user=Depends(require_audit_read)
):
    from sqlalchemy import select

    from app.db.models import AuditEvent
    from app.services.audit.service import chain_status

    screening = screening_service.require_screening(db, run_id)
    events = list(
        db.execute(
            select(AuditEvent)
            .where(AuditEvent.screening_id == screening.id)
            .order_by(AuditEvent.created_at.asc())
        ).scalars()
    )
    return ok(
        {
            "run_id": run_id,
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
            "chain": chain_status(),
            "disclaimer": (
                "The chain stores hashes only — never PII. Anchors remain 'pending' "
                "while the local EVM node is offline; the DB audit trail is always active."
            ),
        }
    )


# ---------------------------------------------------------------------------
# GET /api/screening/{run_id}/file  — serve stored document for preview
# ---------------------------------------------------------------------------
@router.get("/{run_id}/file")
def get_screening_file(
    run_id: str, db: DbDep = None, _user=Depends(require_screening_read)
):
    screening = screening_service.require_screening(db, run_id)
    path = Path(screening.original_path)
    if not path.is_file():
        raise AppError("Stored file not found on disk", 404)
    return FileResponse(path, filename=path.name)


@router.get("/{run_id}/probe-file")
def get_screening_probe_file(
    run_id: str, db: DbDep = None, _user=Depends(require_screening_read)
):
    """Serve the stored presented-person image for preview."""
    screening = screening_service.require_screening(db, run_id)
    if not screening.probe_image_path:
        raise AppError("No presented-person image stored for this run", 404)
    path = Path(screening.probe_image_path)
    if not path.is_file():
        raise AppError("Stored probe file not found on disk", 404)
    return FileResponse(path, filename=path.name)
