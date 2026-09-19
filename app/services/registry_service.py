"""MOCK document registry lookups.

EVERYTHING HERE IS DEMO DATA. This service simulates what a real
government-registry connector *would* return. There is NO connection to
any government database. Every response is stamped ``"mock": True`` so
demo data can never masquerade as authoritative intelligence.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MockRegistryDocument

# Mapping from OCR/doc-type names to registry doc_type values.
SUPPORTED_DOC_TYPES = {
    "passport",
    "visa",
    "national_id",
    "driving_license",
    "permit",
}


def lookup_document(
    session: Session, doc_type: str, doc_number: str
) -> dict | None:
    """Look up a document in the MOCK registry.

    Returns a dict stamped ``"mock": True``, or None when no mock row
    matches (callers must treat None as "not found in mock data", NOT as
    "document is fake").
    """
    doc_type = (doc_type or "").strip().lower()
    doc_number = (doc_number or "").strip().upper()
    if not doc_type or not doc_number:
        return None

    row = session.execute(
        select(MockRegistryDocument).where(
            MockRegistryDocument.doc_type == doc_type,
            MockRegistryDocument.doc_number == doc_number,
        )
    ).scalar_one_or_none()

    if row is None:
        return None

    return {
        "mock": True,  # honesty stamp on every response
        "doc_type": row.doc_type,
        "doc_number": row.doc_number,
        "full_name": row.full_name,
        "date_of_birth": row.date_of_birth,
        "expiry_date": row.expiry_date,
        "status": row.status,
        "is_mock_row": row.is_mock,
    }
