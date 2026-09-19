"""SQLAlchemy engine/session management.

SQLite in dev (zero setup), Postgres in prod via APP_DB_URL.
"""
from __future__ import annotations

from collections.abc import Generator

import logging

from sqlalchemy import Boolean as sqlalchemy_Boolean
from sqlalchemy import DateTime as sqlalchemy_DateTime
from sqlalchemy import Integer as sqlalchemy_Integer
from sqlalchemy import String as sqlalchemy_String
from sqlalchemy import Text as sqlalchemy_Text
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_engine = None
_session_factory: sessionmaker[Session] | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, always closed."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _sync_sqlite_columns() -> None:
    """Add columns that exist on the models but not on an older dev DB.

    Lightweight stand-in for Alembic (later step): ``create_all`` cannot
    alter tables that already exist, so a dev database created before a
    column was introduced would crash on insert. Only NULLABLE column adds
    are performed — safe, additive, idempotent. (Step 11: widened from
    TEXT-only to TEXT/Integer/DateTime/Boolean for the lockout columns.)
    """
    from sqlalchemy import text

    engine = get_engine()
    if not engine.url.get_backend_name().startswith("sqlite"):
        return  # Postgres migrations are handled by Alembic in prod
    from app.db import models  # noqa: F401 - ensure models are registered

    addable_types = (
        sqlalchemy_Text,
        sqlalchemy_String,
        sqlalchemy_Integer,
        sqlalchemy_DateTime,
        sqlalchemy_Boolean,
    )

    with engine.begin() as conn:
        for table in models.Base.metadata.sorted_tables:
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table.name})"))
            }
            if not existing:
                continue  # table doesn't exist yet; create_all handles it
            for column in table.columns:
                if column.name in existing:
                    continue
                if column.nullable and isinstance(column.type, addable_types):
                    conn.execute(
                        text(
                            f"ALTER TABLE {table.name} ADD COLUMN {column.name} "
                            f"{column.type.compile(engine.dialect)}"
                        )
                    )
                    logger.info("Dev migration: added %s.%s", table.name, column.name)


def create_all() -> None:
    """Create all tables + self-heal older dev DBs. (Alembic arrives later.)"""
    from app.db import models  # noqa: F401 - ensure models are registered

    Base = models.Base
    Base.metadata.create_all(get_engine())
    _sync_sqlite_columns()
