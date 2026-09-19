"""Standard API response envelope used by every endpoint."""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.middleware import get_request_id

# Shown on every screening response: this system assists humans, it does
# not replace them.
DISCLAIMER = (
    "AI-assisted decision-support output. Not a legal determination. "
    "Final screening decisions must be made by authorized human personnel."
)


def ok(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    settings = get_settings()
    body = {
        "success": True,
        "app": settings.app_name,
        "version": settings.app_version,
        "mock_mode": settings.mock_mode,
        "request_id": get_request_id(),
        **payload,
    }
    return JSONResponse(status_code=status_code, content=body)


def err(message: str, status_code: int = 400, details: Any = None) -> JSONResponse:
    settings = get_settings()
    body: dict[str, Any] = {
        "success": False,
        "app": settings.app_name,
        "version": settings.app_version,
        "request_id": get_request_id(),
        "error": message,
    }
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status_code, content=body)
