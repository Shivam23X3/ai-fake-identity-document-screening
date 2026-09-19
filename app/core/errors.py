"""Application error type + global exception handlers.

Every error leaves the API in the same JSON envelope:
{"success": false, "error": "...", "request_id": "...", "details": ...}
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.responses import err

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Business-level error with an HTTP status."""

    def __init__(self, message: str, status_code: int = 400, details=None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request, exc: AppError):
        return err(exc.message, exc.status_code, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request, exc: RequestValidationError):
        return err("Request validation failed", 422, jsonable_encoder(exc.errors()))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request, exc: StarletteHTTPException):
        return err(str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unhandled(request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        details = None
        if get_settings().env == "dev":
            details = f"{type(exc).__name__}: {exc}"
        return err("Internal server error", 500, details)
