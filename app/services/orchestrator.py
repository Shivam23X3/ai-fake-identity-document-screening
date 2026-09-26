"""Step 9 — Orchestration service coordinating the complete AI pipeline.

    INPUT → Preprocess → OCR → Validation → Tampering → Face → Risk
          → Final Screening Result → Audit Trail (+ optional chain anchor)

Responsibilities (per the Step-9 requirements):
- **Unique screening ID**: one ``run_id`` (uuid4 hex) per screening, created
  at upload time and reused everywhere (DB row, report, chain anchor).
- **Failure isolation**: every module runs independently behind the stage
  protocol; a crash marks that stage ``error`` and the pipeline continues
  (policy ``skip``). The orchestrator itself wraps the entire run so even a
  pipeline-level catastrophe yields an honest, auditable result.
- **Intermediate results**: every stage payload is persisted verbatim in
  ``screenings.report_json`` (the single source of truth for the API,
  dashboard and audit hashing).
- **Processing time**: wall-clock per stage (``duration_ms``) plus the total
  (``processing_time_ms``).
- **Model confidence**: each stage's confidence is recorded and surfaced in
  ``confidence_summary``; the fused verdict carries its own confidence.
- **Audit trail**: a DB ``audit_events`` row commits
  ``sha256(canonical_json(report))``; a BackgroundTask then attempts the
  on-chain anchor (hashes only — PII never leaves the local DB).

The orchestrator never makes a decision: ``final_status`` is advisory and
``human_review_required`` can only ever be forced ON, never cleared.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.pipeline import Pipeline, PipelineContext, StageStatus
from app.pipelines import build_document_screening_pipeline
from app.services import screening_service
from app.services.audit.hashing import sha256_hex

logger = logging.getLogger(__name__)


class Orchestrator:
    """Runs the full document-screening workflow and persists everything."""

    name = "orchestrator-v1"

    def __init__(self, pipeline: Pipeline | None = None) -> None:
        self._pipeline = pipeline

    @property
    def pipeline(self) -> Pipeline:
        if self._pipeline is None:
            self._pipeline = build_document_screening_pipeline()
        return self._pipeline

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(
        self,
        session: Session,
        screening,  # app.db.models.Screening
    ) -> dict[str, Any]:
        """Execute the complete workflow for one stored screening row.

        Idempotent: a row that is not ``processing`` returns its stored
        consolidated result instead of re-running.
        """
        if screening.status != "processing":
            stored = screening_service.load_report(screening)
            if isinstance(stored, dict) and "final_status" in stored:
                # Idempotent re-read: attach the existing audit event and
                # return the stored result without re-running the pipeline.
                self._attach_audit(None, screening, stored)
                return stored
            # Legacy report (pre-Step-9 shape): re-run for consistency.
            screening.status = "processing"

        started = time.perf_counter()
        ctx = PipelineContext(
            {
                "run_id": screening.run_id,
                "original_path": screening.original_path,
                "probe_image_path": screening.probe_image_path,
                "doc_type_hint": screening.doc_type_hint or "unknown",
                "pdf_pages": getattr(screening, "pdf_pages", None),
            }
        )

        try:
            results = self.pipeline.run(ctx)
            pipeline_error = None
        except Exception as exc:  # noqa: BLE001 - total isolation guarantee
            logger.exception("Pipeline-level failure for run %s", screening.run_id)
            results = []
            pipeline_error = f"{type(exc).__name__}: {exc}"

        stage_dicts = [r.to_dict() for r in results]
        stages_by_name = {s["stage"]: s for s in stage_dicts}

        # --- intermediate results (verbatim stage payloads) -----------------
        # The risk stage's data block is the FULL provider payload (spec keys
        # risk_score/risk_level/reasons + bands); the ctx 'risk' key holds the
        # nested block. Prefer the stage data, fall back to ctx.
        risk = stages_by_name.get("risk_assessment", {}).get("data", {}) or ctx.get("risk") or {}
        face = ctx.get("face_verification") or {}
        tampering = ctx.get("tampering") or {}
        validation = ctx.get("validation") or {}
        ocr = ctx.get("ocr_fields")
        ocr_payload = stages_by_name.get("ocr", {}).get("data", {})
        preprocess = stages_by_name.get("preprocess", {}).get("data", {})

        document = {
            "run_id": screening.run_id,
            "original_path": screening.original_path,
            "file_sha256": screening.file_sha256,
            "doc_type_hint": screening.doc_type_hint,
            "doc_type_detected": ctx.get("doc_type_detected")
            or ocr_payload.get("doc_type_detected"),
            "probe_image_path": screening.probe_image_path,
            "pdf_pages": getattr(screening, "pdf_pages", None),
            "pdf_pages_screened": (1 if getattr(screening, "pdf_pages", None) else None),
        }

        # --- fused verdict ---------------------------------------------------
        review_flags = [bool(s.get("human_review_required")) for s in stage_dicts]
        risk_review = bool(risk.get("human_review_required", True))
        risk_score = risk.get("risk_score", risk.get("score"))
        risk_level = risk.get("risk_level", risk.get("level"))
        risk_band = risk.get("band") or (
            str(risk_level).lower() if risk_level else None
        )
        human_review_required = (
            any(review_flags) or risk_review or pipeline_error is not None
        )

        if pipeline_error is not None:
            final_status = "error"
        elif all(s.get("status") == StageStatus.OK for s in stage_dicts) and stage_dicts:
            final_status = "completed"
        elif any(s.get("status") == StageStatus.ERROR for s in stage_dicts):
            final_status = "completed_with_errors"
        elif any(s.get("status") == StageStatus.SKIPPED for s in stage_dicts):
            final_status = "completed_with_skips"
        else:
            final_status = "completed_with_degraded_modules"

        # --- confidence summary ------------------------------------------------
        def _conf(s: dict[str, Any]) -> float | None:
            c = s.get("confidence")
            return round(float(c), 3) if isinstance(c, (int, float)) else None

        confidence_summary = {
            s["stage"]: _conf(s) for s in stage_dicts if s.get("stage") != "risk_assessment"
        }
        risk_conf = risk.get("confidence")
        confidence_summary["risk_assessment"] = (
            round(float(risk_conf), 3) if isinstance(risk_conf, (int, float)) else None
        )

        processing_time_ms = int((time.perf_counter() - started) * 1000)

        result = {
            # --- Step-9 contract (spec) -----------------------------------------
            "screening_id": screening.run_id,
            "document": document,
            "ocr": {
                "fields": ocr,
                "mrz_raw": ocr_payload.get("mrz_raw"),
                "doc_type_detected": ocr_payload.get("doc_type_detected"),
                "overall_confidence": ocr_payload.get("overall_confidence"),
                "engine": ocr_payload.get("engine"),
            },
            "validation": validation if isinstance(validation, dict) else {},
            "tampering": tampering if isinstance(tampering, dict) else {},
            "face_verification": face if isinstance(face, dict) else {},
            "risk_assessment": {
                "risk_score": risk_score,
                "risk_level": risk_level,
                "reasons": risk.get("reasons", []),
                "contributions": risk.get("contributions", []),
                "confidence": confidence_summary["risk_assessment"],
                "bands": risk.get("bands"),
                "human_review_required": risk_review,
            },
            "final_status": final_status,
            "human_review_required": human_review_required,
            "processing_time_ms": processing_time_ms,
            # --- orchestration metadata ------------------------------------------
            "orchestrator": self.name,
            "stages": stage_dicts,
            "confidence_summary": confidence_summary,
            "pipeline_error": pipeline_error,
            "disclaimer": (
                "AI-assisted decision support only. This result is advisory, "
                "carries per-stage confidence, and NEVER constitutes an "
                "automatic accept/reject. Final decisions rest with "
                "authorized human personnel."
            ),
        }

        # --- persistence + audit trail ---------------------------------------
        # The stored report deliberately EXCLUDES the audit block: the audit
        # hash commits to the pure pipeline output, so verification can
        # re-hash the stored report at any time (idempotent, no drift).
        screening_service.save_report(
            session,
            screening,
            report=result,
            doc_type_detected=result["document"]["doc_type_detected"],
            risk_score=(risk_score if isinstance(risk_score, (int, float)) else None),
            risk_band=risk_band,
            human_review_required=human_review_required,
        )
        self._attach_audit(session, screening, result)

        return result

    # ------------------------------------------------------------------
    # Audit-trail helpers
    # ------------------------------------------------------------------
    def _attach_audit(self, session: Session | None, screening, result: dict[str, Any]) -> None:
        """Create (fresh runs) or look up (re-reads) the audit event and attach
        its summary to the in-memory result. The stored report stays
        audit-block-free so its hash remains verifiable."""
        try:
            from sqlalchemy import select

            from app.db.models import AuditEvent
            from app.services.audit.service import record_screening_completed

            own_session = session is None
            if own_session:
                from app.db.base import get_session_factory

                session = get_session_factory()()
            try:
                # A screening accumulates MULTIPLE audit events once Step-10
                # logging adds 'audit_logged' rows: pick the freshest
                # 'screening_completed' deterministically instead of
                # scalar_one_or_none() (MultipleResultsFound). The hash of
                # that event is the one committed at analyze time, so it is
                # also the correct one to re-verify.
                event = session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.screening_id == screening.id)
                    .where(AuditEvent.event_type == "screening_completed")
                    .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                ).scalars().first()
                if event is None:
                    event = record_screening_completed(
                        session,
                        screening_id=screening.id,
                        run_id=screening.run_id,
                        report=result,  # result has no 'audit' key at this point
                    )
                    if own_session:
                        session.commit()
                result["audit"] = {
                    "event_id": event.id,
                    "payload_sha256": event.payload_sha256,
                    "anchor_status": event.anchor_status,
                }
            finally:
                if own_session:
                    session.close()
        except Exception as exc:  # noqa: BLE001 - audit must never kill the run
            logger.warning("Audit-trail write failed for %s: %s", screening.run_id, exc)
            result["audit"] = {"error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Small JSON helpers (import-time-safe)
# ---------------------------------------------------------------------------
def json_loads(raw: str | None) -> Any:
    import json

    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, default=str)


__all__ = ["Orchestrator", "json_dumps", "json_loads"]
