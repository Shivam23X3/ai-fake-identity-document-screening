"""Investigation & intelligence service (Step 12).

Powers the Investigation & Intelligence dashboard:

- **Search** stored screenings with filters (text, risk band, document
  type, date range, human-review flag, pagination) — see
  :func:`search_screenings`.
- **Aggregate** the corpus into anonymized chart statistics — see
  :func:`get_stats`. Every aggregate returns ``(label, count)`` pairs only;
  no names, document numbers, MRZ lines or images are ever included.
- **Inspect** one case: risk contributions, validation failures, tampering
  indicators, face-verification verdict — see :func:`inspect_case`. The
  payload is a deliberately whitelisted projection of the stored Step-9
  report: each module contributes only its decision fields, not its raw
  OCR data (the full report stays available to authorized roles through
  ``GET /api/screening/{run_id}``).

Honesty rules carried over from earlier steps:
- Every response carries the advisory disclaimer.
- Aggregates never extrapolate beyond the filtered corpus they were
  computed from — ``window`` echoes the exact filter parameters used.
- ``raw_report`` is only added when ``include_full_report=True`` (ADMIN /
  explicit detail requests); the default projection is the minimal set.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.core.responses import DISCLAIMER
from app.db.models import AuditEvent, Screening

# Doc types accepted by the upload endpoint (same allowlist — the dashboard
# cannot filter by a type the pipeline cannot produce).
DOC_TYPES = ("passport", "visa", "national_id", "driving_license", "permit", "unknown")
RISK_BANDS = ("low", "medium", "high", "critical")

MAX_PAGE_SIZE = 100


def _matches_text(screening: Screening, needle: str) -> bool:
    """Case-insensitive run-id / file-hash / doc-type text search.

    PII-safe by construction: the only searchable text fields are the
    opaque run_id, the file sha256 and document-type labels — NOT the
    encrypted OCR payload (names/document numbers are not searchable).
    """
    needle = needle.lower()
    return bool(
        needle
        and (
            needle in (screening.run_id or "").lower()
            or needle in (screening.file_sha256 or "").lower()
            or needle in (screening.doc_type_hint or "").lower()
            or needle in (screening.doc_type_detected or "").lower()
        )
    )


def _apply_filters(
    stmt,  # Select[type[Screening]]
    *,
    q: str | None,
    risk_band: str | None,
    doc_type: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    review_required: bool | None,
    status: str | None,
):
    """Compose the shared WHERE clause for search + stats (same filters
    must yield the same corpus, otherwise charts and the table disagree)."""
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Screening.run_id).like(like),
                func.lower(Screening.file_sha256).like(like),
                func.lower(Screening.doc_type_hint).like(like),
                func.lower(Screening.doc_type_detected).like(like),
            )
        )
    if risk_band:
        stmt = stmt.where(Screening.risk_band == risk_band)
    if doc_type:
        stmt = stmt.where(
            or_(
                func.lower(cast(Screening.doc_type_detected, String)) == doc_type,
                func.lower(cast(Screening.doc_type_hint, String)) == doc_type,
            )
        )
    if date_from is not None:
        stmt = stmt.where(Screening.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Screening.created_at <= date_to)
    if review_required is not None:
        stmt = stmt.where(Screening.human_review_required == review_required)
    if status:
        stmt = stmt.where(Screening.status == status)
    return stmt


def _parse_date_bound(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    """Parse a YYYY-MM-DD query param into an aware UTC datetime.

    ``end_of_day`` makes the inclusive upper bound cover the whole day
    (23:59:59.999999 UTC) so ``date_to=2026-09-19`` includes runs later
    that day. Invalid values return None (ignored) rather than erroring —
    dashboard filters must never hard-fail a demo.
    """
    if not value:
        return None
    try:
        day = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    if end_of_day:
        return datetime.combine(day, time.max, tzinfo=timezone.utc)
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _row_summary(row: Screening) -> dict[str, Any]:
    """The PII-minimal row shape shared by search results and the table.

    Deliberately excludes: operator identity, file paths, the OCR payload,
    and anything else that could identify a person.
    """
    return {
        "run_id": row.run_id,
        "doc_type_hint": row.doc_type_hint,
        "doc_type_detected": row.doc_type_detected,
        "status": row.status,
        "risk_score": row.risk_score,
        "risk_band": row.risk_band,
        "human_review_required": bool(row.human_review_required),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def search_screenings(
    session: Session,
    *,
    q: str | None = None,
    risk_band: str | None = None,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    review_required: bool | None = None,
    status: str | None = None,
    sort: str = "newest",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Filtered, paginated screening search (PII-minimal rows).

    Sort: ``newest`` (default) | ``oldest`` | ``risk_desc`` | ``risk_asc``.
    """
    stmt = _apply_filters(
        select(Screening),
        q=q,
        risk_band=risk_band,
        doc_type=doc_type,
        date_from=_parse_date_bound(date_from),
        date_to=_parse_date_bound(date_to, end_of_day=True),
        review_required=review_required,
        status=status,
    )

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    order: tuple = {
        "newest": (Screening.created_at.desc(),),
        "oldest": (Screening.created_at.asc(),),
        "risk_desc": (Screening.risk_score.desc().nullslast(), Screening.created_at.desc()),
        "risk_asc": (Screening.risk_score.asc().nullslast(), Screening.created_at.desc()),
    }.get(sort, (Screening.created_at.desc(),))

    stmt = stmt.order_by(*order).limit(min(limit, MAX_PAGE_SIZE)).offset(max(offset, 0))
    rows = list(session.execute(stmt).scalars())

    filters_echo = {
        "q": q,
        "risk_band": risk_band,
        "doc_type": doc_type,
        "date_from": date_from,
        "date_to": date_to,
        "review_required": review_required,
        "status": status,
        "sort": sort,
    }
    return {
        "items": [_row_summary(r) for r in rows],
        "total": int(total),
        "limit": min(limit, MAX_PAGE_SIZE),
        "offset": max(offset, 0),
        "count": len(rows),
        "filters_applied": {k: v for k, v in filters_echo.items() if v is not None},
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Aggregates for visualizations
# ---------------------------------------------------------------------------
def _day_key(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else "unknown"


def get_stats(
    session: Session,
    *,
    q: str | None = None,
    risk_band: str | None = None,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    review_required: bool | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Chart statistics over the SAME filtered corpus as search_screenings.

    All aggregates are (label, count) pairs — inherently PII-free:
    - screenings_per_day: last 14 calendar days (zero-filled) + older count
    - risk_distribution: LOW / MEDIUM / HIGH / CRITICAL counts
    - document_type_distribution: per doc-type counts
    - validation: VALID / INVALID / UNKNOWN (is_valid flag of stored report)
    - tampering: verdict distribution (clean/suspicious/likely/inconclusive/unknown)
    - face: MATCH / NO_MATCH / INCONCLUSIVE / not performed
    - human_review: flagged vs not-flagged
    - status: pipeline status distribution
    - integrity: computed over the full corpus, independent of filters
    """
    filters = dict(
        q=q,
        risk_band=risk_band,
        doc_type=doc_type,
        date_from=_parse_date_bound(date_from),
        date_to=_parse_date_bound(date_to, end_of_day=True),
        review_required=review_required,
        status=status,
    )
    base = _apply_filters(select(Screening), **filters)
    total = int(session.execute(select(func.count()).select_from(base.subquery())).scalar_one())

    # --- screenings per day (last 14 days, zero-filled) ---------------------
    # func.date() works on both SQLite and Postgres for DateTime columns.
    per_day_raw = dict(
        session.execute(
            _apply_filters(
                select(func.date(Screening.created_at).label("day"), func.count())
                .group_by(func.date(Screening.created_at)),
                **filters,
            )
        ).all()
    )
    today = datetime.now(timezone.utc).date()
    days = [(today.fromordinal(today.toordinal() - i)).isoformat() for i in range(13, -1, -1)]
    per_day = [{"day": d, "count": int(per_day_raw.get(d, 0))} for d in days]
    older = sum(int(v) for k, v in per_day_raw.items() if str(k) < days[0])
    screenings_per_day = {"days": per_day, "window_days": 14, "older_than_window": older}

    # --- risk distribution ---------------------------------------------------
    band_rows = session.execute(
        _apply_filters(
            select(Screening.risk_band, func.count()).group_by(Screening.risk_band),
            **filters,
        )
    ).all()
    risk_distribution = {band: 0 for band in RISK_BANDS}
    risk_distribution["unassessed"] = 0
    for band, n in band_rows:
        key = (band or "").lower()
        risk_distribution[key if key in RISK_BANDS else "unassessed"] += int(n)

    # --- document type distribution ------------------------------------------
    dtype_rows = session.execute(
        _apply_filters(
            select(
                func.coalesce(func.lower(Screening.doc_type_detected), func.lower(Screening.doc_type_hint)),
                func.count(),
            ).group_by(
                func.coalesce(func.lower(Screening.doc_type_detected), func.lower(Screening.doc_type_hint))
            ),
            **filters,
        )
    ).all()
    doc_type_distribution = {t: 0 for t in DOC_TYPES}
    for dtype, n in dtype_rows:
        key = (dtype or "unknown").lower()
        doc_type_distribution[key if key in DOC_TYPES else "unknown"] += int(n)

    # --- module outcomes: scan the stored reports (module blocks only) -------
    # The filtering query returns matching Screening ROWS; module verdicts
    # live inside the (encrypted) report_json, so the scan decrypts the
    # report and reads ONLY the decision fields of each module. Aggregate
    # counters only — nothing per-case is retained after this function.
    module_stats = {
        "validation": {"valid": 0, "invalid": 0, "unknown": 0},
        "tampering": {
            "no_obvious_manipulation": 0, "suspicious": 0, "likely_manipulated": 0,
            "inconclusive": 0, "unknown": 0,
        },
        "face": {"match": 0, "no_match": 0, "inconclusive": 0, "not_performed": 0},
    }
    rows_for_modules = list(session.execute(base).scalars())
    from app.services import screening_service

    for row in rows_for_modules:
        try:
            report = screening_service.load_report(row)
        except Exception:  # noqa: BLE001 - a corrupt row must not kill the page
            report = {}
        validation = report.get("validation") if isinstance(report, dict) else None
        if isinstance(validation, dict) and "is_valid" in validation:
            module_stats["validation"]["valid" if validation.get("is_valid") else "invalid"] += 1
        else:
            module_stats["validation"]["unknown"] += 1

        tampering = report.get("tampering") if isinstance(report, dict) else None
        verdict = (tampering or {}).get("verdict") if isinstance(tampering, dict) else None
        module_stats["tampering"][
            verdict if verdict in module_stats["tampering"] else "unknown"
        ] += 1

        face = report.get("face_verification") if isinstance(report, dict) else None
        match_status = (face or {}).get("match_status") if isinstance(face, dict) else None
        if match_status in {"MATCH", "NO_MATCH", "INCONCLUSIVE"}:
            module_stats["face"][match_status.lower()] += 1
        else:
            module_stats["face"]["not_performed"] += 1

    # --- human-review + status ------------------------------------------------
    review_rows = session.execute(
        _apply_filters(
            select(Screening.human_review_required, func.count()).group_by(
                Screening.human_review_required
            ),
            **filters,
        )
    ).all()
    human_review = {str(bool(flag)).lower(): int(n) for flag, n in review_rows}
    human_review_casefold = {
        "true": human_review.get("true", 0),
        "false": human_review.get("false", 0),
    }

    status_rows = session.execute(
        _apply_filters(
            select(Screening.status, func.count()).group_by(Screening.status),
            **filters,
        )
    ).all()
    status_distribution = {str(s or "unknown"): int(n) for s, n in status_rows}

    # --- integrity (over the FULL corpus; filters do not change this) ---------
    total_all = int(
        session.execute(select(func.count()).select_from(Screening)).scalar_one()
    )
    anchored_all = int(
        session.execute(
            select(func.count())
            .select_from(Screening)
            .join(AuditEvent, AuditEvent.screening_id == Screening.id)
            .where(AuditEvent.event_type == "screening_completed")
            .where(AuditEvent.anchor_status == "anchored")
        ).scalar_one()
    )
    logged_all = int(
        session.execute(
            select(func.count())
            .select_from(Screening)
            .join(AuditEvent, AuditEvent.screening_id == Screening.id)
            .where(AuditEvent.event_type == "audit_logged")
        ).scalar_one()
    )
    integrity = {
        "total_screenings": total_all,
        "anchored_on_chain": anchored_all,
        "audit_logged": logged_all,
        "pending": max(0, total_all - anchored_all),
        "note": (
            "Computed over the full corpus, independent of the filters above. "
            "Anchors honestly stay 'pending' while the local EVM node is offline."
        ),
    }

    return {
        "window": {
            "q": q,
            "risk_band": risk_band,
            "doc_type": doc_type,
            "date_from": date_from,
            "date_to": date_to,
            "review_required": review_required,
            "status": status,
        },
        "total": total,
        "human_review": human_review_casefold,
        "screenings_per_day": screenings_per_day,
        "risk_distribution": risk_distribution,
        "doc_type_distribution": doc_type_distribution,
        "validation": module_stats["validation"],
        "tampering": module_stats["tampering"],
        "face": module_stats["face"],
        "status": status_distribution,
        "integrity": integrity,
        "privacy_note": (
            "All statistics are aggregate (label, count) pairs over stored "
            "screening metadata — no names, document numbers, MRZ text, "
            "images or biometric data are included in any field."
        ),
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Case inspector
# ---------------------------------------------------------------------------
# Whitelisted indicator fields per module — the projection stays useful for
# investigation while excluding raw OCR text (names, document numbers, MRZ).
_FACE_KEYS = (
    "match_status", "verdict", "similarity_score", "confidence",
    "face_detected_document", "face_detected_presented_person",
    "quality_issues", "warnings", "embedding_backend", "one_to_one_only",
    "no_population_search", "disclaimer",
)
_TAMPER_KEYS = (
    "tampering_detected", "risk_score", "confidence", "verdict",
    "verdict_confidence", "agreeing_types", "coverage",
    "detector_failures", "human_review_required", "explanation", "disclaimer",
)
_VAL_KEYS = (
    "is_valid", "confidence", "doc_type", "ruleset", "failures", "warnings",
    "notes",
)


def _checks_projection(validation: dict[str, Any]) -> list[dict[str, Any]]:
    """Validation checks without raw OCR values — (field, status, message)."""
    out: list[dict[str, Any]] = []
    for chk in validation.get("checks") or []:
        if isinstance(chk, dict):
            out.append(
                {
                    "field": chk.get("field"),
                    "status": chk.get("status"),
                    "message": chk.get("message"),
                }
            )
    return out


def _indicators_projection(tampering: dict[str, Any]) -> list[dict[str, Any]]:
    """Tampering indicators: type/severity/confidence/note only."""
    out: list[dict[str, Any]] = []
    for ind in tampering.get("indicators") or []:
        if isinstance(ind, dict):
            out.append(
                {
                    "type": ind.get("type"),
                    "severity": ind.get("severity"),
                    "confidence": ind.get("confidence"),
                    "note": str(ind.get("note", ""))[:200],
                }
            )
    return out


def inspect_case(
    session: Session,
    run_id: str,
    *,
    include_full_report: bool = False,
) -> dict[str, Any]:
    """The investigation view of ONE case (whitelisted module projection).

    Includes the blockchain-integrity verdict for this screening (recomputed
    hash vs the ledger commitment) and the local audit event trail.
    """
    from app.services import screening_service
    from app.services.audit import service as audit_service

    screening = screening_service.require_screening(session, run_id)
    report = screening_service.load_report(screening)
    if not isinstance(report, dict) or "final_status" not in report:
        raise ValueError("not_analyzed")

    risk = report.get("risk_assessment") or {}
    validation = report.get("validation") or {}
    tampering = report.get("tampering") or {}
    face = report.get("face_verification") or {}

    def pick(src: dict[str, Any], keys) -> dict[str, Any]:
        return {k: src[k] for k in keys if k in src}

    case = {
        "run_id": screening.run_id,
        "created_at": screening.created_at.isoformat() if screening.created_at else None,
        "completed_at": screening.completed_at.isoformat() if screening.completed_at else None,
        "status": screening.status,
        "doc_type_hint": screening.doc_type_hint,
        "doc_type_detected": screening.doc_type_detected,
        "file_sha256": screening.file_sha256,
        "human_review_required": bool(screening.human_review_required),
        "final_status": report.get("final_status"),
        "processing_time_ms": report.get("processing_time_ms"),
        "confidence_summary": report.get("confidence_summary"),
        "pipeline_error": report.get("pipeline_error"),
        "stages": [
            {
                "stage": s.get("stage"),
                "status": s.get("status"),
                "confidence": s.get("confidence"),
                "human_review_required": bool(s.get("human_review_required")),
                "duration_ms": s.get("duration_ms"),
            }
            for s in (report.get("stages") or [])
            if isinstance(s, dict)
        ],
        "risk": {
            "risk_score": risk.get("risk_score"),
            "risk_level": risk.get("risk_level"),
            "reasons": risk.get("reasons", []),
            "contributions": risk.get("contributions", []),
            "confidence": risk.get("confidence"),
            "routing_reasons": risk.get("routing_reasons", []),
        },
        "validation": {
            **pick(validation, _VAL_KEYS),
            "checks": _checks_projection(validation),
        },
        "tampering": {
            **pick(tampering, _TAMPER_KEYS),
            "indicators": _indicators_projection(tampering),
        },
        "face": pick(face, _FACE_KEYS),
    }

    # --- blockchain audit integrity for THIS case ---------------------------
    case["audit"] = {"events": [], "chain": None, "verify": None, "note": None}
    try:
        events = list(
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.screening_id == screening.id)
                .order_by(AuditEvent.created_at.asc())
            ).scalars()
        )
        case["audit"]["events"] = [
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
        ]
        case["audit"]["chain"] = audit_service.chain_status()
        verify = audit_service.verify_audit_record(session, screening)
        case["audit"]["verify"] = {
            "verified": verify.get("verified"),
            "on_ledger": verify.get("on_ledger"),
            "record_changed": verify.get("record_changed"),
            "conclusion": verify.get("conclusion"),
            "ledger_backend": verify.get("ledger_backend"),
        }
    except Exception as exc:  # noqa: BLE001 - integrity check is advisory
        case["audit"] = {
            "events": [],
            "chain": None,
            "verify": None,
            "note": f"Integrity check unavailable: {str(exc)[:150]}",
        }

    if include_full_report:
        case["raw_report"] = report

    case["disclaimer"] = DISCLAIMER
    return case
