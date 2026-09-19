"""Request context middleware: X-Request-ID + access logging + security headers."""
from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

access_logger = logging.getLogger("access")

# Security headers applied to every response (Step 11).
# - nosniff/referrer/XFO: classic hardening; CSP even for API responses
#   (browsers render error bodies); COOP/CORP isolate browsing contexts;
#   HSTS only when the request arrived over TLS (dev is plain HTTP).
_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",  # never cache screening results/PII
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach hardened response headers; HSTS added on https requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response


def get_request_id() -> str:
    try:
        return request_id_var.get()
    except LookupError:  # pragma: no cover - outside request context
        return "-"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attaches a request id to every request/response and logs access."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            access_logger.exception(
                "%s %s -> 500 (unhandled) rid=%s",
                request.method,
                request.url.path,
                rid,
            )
            request_id_var.reset(token)
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        access_logger.info(
            "%s %s -> %d %dms rid=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            rid,
        )
        response.headers["X-Request-ID"] = rid
        request_id_var.reset(token)
        return response
