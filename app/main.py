"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.rate_limit import limiter, rate_limit_handler
from app.api.routes_health import router as health_router
from app.api.routes_screening import router as screening_router
from app.api.routes_audit import router as audit_router
from app.api.routes_auth import router as auth_router
from app.api.routes_investigation import router as investigation_router
from app.api.routes_demo import router as demo_router
from app.api.routes_review import router as review_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()

    # Database: create tables + seed the clearly-marked MOCK registry.
    from app.db.base import create_all
    from app.db.base import get_session_factory
    from app.db.seed_mock_registry import seed_mock_registry

    create_all()
    seed_mock_registry(get_session_factory()())

    # Step 11: create the first ADMIN from env credentials (empty table only).
    from app.services.user_service import bootstrap_admin

    bootstrap_admin(get_session_factory()())

    # Step 11: best-effort cleanup of expired uploads from prior runs.
    from app.services.cleanup_service import cleanup_expired_uploads

    removed = cleanup_expired_uploads()
    if removed:
        logger.info("Startup cleanup removed %d expired upload(s).", removed)

    if settings.mock_mode:
        logger.warning(
            "MOCK_MODE is ON: all registry lookups use clearly-marked mock data. "
            "There is NO connection to any government database."
        )
    if settings.env != "prod" and not settings.encryption_key:
        logger.warning(
            "Encryption at rest is OFF (no APP_ENCRYPTION_KEY): report_json with "
            "OCR fields is stored PLAINTEXT. Set APP_ENCRYPTION_KEY (required in prod)."
        )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level, log_dir=settings.logs_dir if settings.env != "test" else None)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "AI-assisted decision-support system for screening identity and "
            "travel documents (passport, visa, national ID, driving license, "
            "permits). **All outputs are advisory: final decisions rest with "
            "authorized human personnel.**"
        ),
        lifespan=lifespan,
    )

    # --- Middleware (order matters: outermost first) ---
    app.add_middleware(RequestContextMiddleware)  # request ids + access log
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        # Step 11: explicit origins, credentials OFF (Bearer tokens, not
        # cookies, so credentialed cross-site requests are not a thing here),
        # wildcard "*" deliberately avoided.
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    # --- Rate limiting (Step 11): per-IP, headers + 429 handler ---
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    app.add_middleware(SlowAPIMiddleware)

    # --- Routers ---
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(screening_router)
    app.include_router(audit_router)
    app.include_router(investigation_router)
    app.include_router(demo_router)
    app.include_router(review_router)

    # --- Uniform error envelope ---
    register_exception_handlers(app)
    return app


app = create_app()
