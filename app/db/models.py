"""ORM models — schema matches ARCHITECTURE.md §5."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Brute-force lockout accounting (Step 11). failed_login_count resets on
    # success; locked_until is a UTC timestamp past which login is allowed.
    failed_login_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # True for the env-bootstrapped ADMIN until the first password change:
    # such logins get a 60-second token so the bootstrap credential cannot
    # become a standing one.
    must_change_password: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Screening(Base):
    __tablename__ = "screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional live/presented-person capture used for 1:1 face verification
    # (Step 7). Stored next to the document under data/uploads/<run_id>/.
    probe_image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_type_detected: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="processing")
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_band: Mapped[str | None] = mapped_column(Text, nullable=True)
    human_review_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Step 11: app-level Fernet encryption (APP_ENCRYPTION_KEY). Flag records
    # whether THIS row's payload is encrypted — reads stay correct for legacy
    # plaintext rows written before the key was configured.
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable (not False) so the dev auto-migration can add this column to
    # pre-existing DBs; legacy rows read NULL → falsy → plaintext, which is
    # exactly right for rows written before encryption was enabled.
    report_encrypted: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screening_id: Mapped[int] = mapped_column(
        ForeignKey("screenings.id"), unique=True, nullable=False
    )
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screening_id: Mapped[int] = mapped_column(ForeignKey("screenings.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    chain: Mapped[str] = mapped_column(Text, default="hardhat-local")
    tx_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anchor_status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MockRegistryDocument(Base):
    __tablename__ = "mock_registry_documents"
    __table_args__ = (UniqueConstraint("doc_type", "doc_number", name="uq_mock_doc"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Schema-level honesty marker: rows in this table MUST be fake data.
    is_mock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    doc_number: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    date_of_birth: Mapped[str | None] = mapped_column(Text, nullable=True)
    expiry_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
