"""Loads the MOCK registry with obviously-fake demo documents.

ALL DATA HERE IS SYNTHETIC. The pattern "MOCK-" in every document number
makes accidental real-world use immediately visible. This table simulates
what a real registry lookup would return; there is NO connection to any
government database.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MockRegistryDocument

logger = logging.getLogger(__name__)

MOCK_DOCUMENTS: list[dict] = [
    {
        "doc_type": "passport",
        "doc_number": "MOCK-P1234567",
        "full_name": "TEST PERSON ONE",
        "date_of_birth": "1990-01-01",
        "expiry_date": "2030-06-30",
        "status": "active",
    },
    {
        "doc_type": "passport",
        "doc_number": "MOCK-P7654321",
        "full_name": "TEST PERSON TWO",
        "date_of_birth": "1985-05-12",
        "expiry_date": "2020-01-15",
        "status": "expired",
    },
    {
        "doc_type": "visa",
        "doc_number": "MOCK-V5551234",
        "full_name": "TEST PERSON THREE",
        "date_of_birth": "1992-11-23",
        "expiry_date": "2027-03-31",
        "status": "active",
    },
    {
        "doc_type": "national_id",
        "doc_number": "MOCK-N9876543",
        "full_name": "TEST PERSON FOUR",
        "date_of_birth": "1978-07-04",
        "expiry_date": "2029-12-31",
        "status": "active",
    },
    {
        "doc_type": "driving_license",
        "doc_number": "MOCK-D1122334",
        "full_name": "TEST PERSON FIVE",
        "date_of_birth": "2000-02-29",
        "expiry_date": "2026-05-20",
        "status": "reported_lost",
    },
    {
        "doc_type": "permit",
        "doc_number": "MOCK-PR9988776",
        "full_name": "TEST PERSON SIX",
        "date_of_birth": "1995-09-09",
        "expiry_date": "2026-01-01",
        "status": "expired",
    },
]


def seed_mock_registry(session: Session) -> int:
    """Idempotent seeding. Returns the number of rows added."""
    added = 0
    for doc in MOCK_DOCUMENTS:
        exists = session.execute(
            select(MockRegistryDocument).where(
                MockRegistryDocument.doc_type == doc["doc_type"],
                MockRegistryDocument.doc_number == doc["doc_number"],
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(MockRegistryDocument(is_mock=True, **doc))
            added += 1
    session.commit()
    logger.info("Mock registry seeding complete (%d added; all rows marked is_mock=1)", added)
    return added
