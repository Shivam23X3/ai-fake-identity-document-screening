"""Step-12 Investigation & Intelligence endpoints.

    GET /api/investigation/search       → filtered, paginated case search
    GET /api/investigation/stats        → aggregate chart statistics
    GET /api/investigation/cases/{run_id} → whitelisted case inspection
                                          (+ blockchain integrity verdict)

Access control (mirrors the RBAC matrix, Step 11):
- search + case inspection require ``screening:read`` (officer/reviewer/admin) —
  the same permission that already guards ``GET /api/screening/{run_id}``;
- aggregates require ``audit:read`` (the charts expose system-wide integrity
  posture, which reviewers and officers already see per-case).

Privacy rules:
- search rows and aggregates carry metadata + (label, count) pairs only —
  never names, document numbers, MRZ text, images or biometric data;
- the case inspector returns a whitelisted module projection (risk
  contributions, validation failures, tampering indicators, face verdict);
  the full stored report remains available to authorized roles via
  ``GET /api/screening/{run_id}`` and is only inlined here with
  ``include_full_report=true``.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.rate_limit import limiter
from app.core.responses import ok
from app.core.security import require_audit_read, require_screening_read
from app.db.base import get_db
from app.services import investigation_service as inv

router = APIRouter(prefix="/api/investigation", tags=["investigation"])

DbDep = Annotated[Session, Depends(get_db)]


def _query_filters(
    q: Annotated[str | None, Query(max_length=100)] = None,
    risk_band: Annotated[
        str | None, Query(description="low | medium | high | critical")
    ] = None,
    doc_type: Annotated[
        str | None, Query(description="passport | visa | national_id | driving_license | permit | unknown")
    ] = None,
    date_from: Annotated[str | None, Query(description="YYYY-MM-DD (inclusive, UTC)")] = None,
    date_to: Annotated[str | None, Query(description="YYYY-MM-DD (inclusive, UTC)")] = None,
    review_required: Annotated[bool | None, Query()] = None,
    status: Annotated[str | None, Query(max_length=40)] = None,
):
    """Shared filter params for search + stats (identical corpus semantics)."""
    if risk_band is not None and risk_band not in inv.RISK_BANDS:
        raise AppError(
            f"risk_band must be one of {list(inv.RISK_BANDS)}", 422
        )
    if doc_type is not None and doc_type not in inv.DOC_TYPES:
        raise AppError(f"doc_type must be one of {list(inv.DOC_TYPES)}", 422)
    if status is not None and any(ch in status for ch in "%_"):
        raise AppError("status must not contain wildcard characters", 422)
    return {
        "q": (q or "").strip() or None,
        "risk_band": risk_band,
        "doc_type": doc_type,
        "date_from": date_from,
        "date_to": date_to,
        "review_required": review_required,
        "status": status,
    }


@router.get("/search")
@limiter.limit("60/minute")
def search(
    request: Request,
    db: DbDep = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Annotated[
        str, Query(description="newest | oldest | risk_desc | risk_asc")
    ] = "newest",
    _filters: dict = Depends(_query_filters),
    _user=Depends(require_screening_read),
):
    """Filtered, paginated search over stored screenings (PII-minimal rows)."""
    if sort not in {"newest", "oldest", "risk_desc", "risk_asc"}:
        raise AppError("sort must be newest | oldest | risk_desc | risk_asc", 422)
    return ok(inv.search_screenings(db, sort=sort, limit=limit, offset=offset, **_filters))


@router.get("/stats")
@limiter.limit("60/minute")
def stats(
    request: Request,
    db: DbDep = None,
    _filters: dict = Depends(_query_filters),
    _user=Depends(require_audit_read),
):
    """Aggregate chart statistics over the same filtered corpus as /search.

    Every value is an aggregate count — no per-case data leaves the DB.
    """
    return ok(inv.get_stats(db, **_filters))


@router.get("/cases/{run_id}")
@limiter.limit("60/minute")
def case(
    request: Request,
    run_id: str,
    db: DbDep = None,
    include_full_report: Annotated[
        bool, Query(description="Inline the full stored report (authorized roles only)")
    ] = False,
    _user=Depends(require_screening_read),
):
    """Inspect ONE suspicious case: risk factors, validation failures,
    tampering indicators, face-verification result — plus the blockchain
    integrity verdict for this screening."""
    try:
        return ok(inv.inspect_case(db, run_id, include_full_report=include_full_report))
    except ValueError as exc:
        if str(exc) == "not_analyzed":
            raise AppError(
                "This run has not been analyzed yet — POST /api/screening/analyze first.",
                409,
            ) from exc
        raise
