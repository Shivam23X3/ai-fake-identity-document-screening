"""API rate limiting (Step 11) — slowapi, per-IP, in-process storage.

Security decisions:

- **Why rate limit**: credential stuffing (login) and resource exhaustion
  (upload/analyze run heavy CV pipelines) are the two cheap DoS/brute-force
  vectors; per-IP limits blunt both. In-process memory storage fits the
  single-node prototype; a multi-node deploy would move storage to Redis.
- **Stricter limits on expensive/dangerous routes**: login 10/min (with
  account lockout behind it), upload 30/min, analyze 20/min — the pipeline
  takes seconds of CPU per run.
- **429s are honest**: the handler returns the standard error envelope with
  Retry-After so well-behaved clients can back off.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings

_settings = get_settings()

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[_settings.rate_limit_default],
    # Behind a reverse proxy, X-Forwarded-For would be needed; direct dev
    # deployment uses the socket address (harder to spoof than a header).
    headers_enabled=True,
)


def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Uniform 429 envelope + Retry-After (slowapi provides `retry_after`)."""
    retry_after = getattr(exc, "retry_after", None) or 60
    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "error": f"Rate limit exceeded ({exc.detail}). Slow down and retry.",
            "request_id": request.headers.get("X-Request-ID", "-"),
        },
        headers={"Retry-After": str(int(retry_after))},
    )


__all__ = ["limiter", "rate_limit_handler"]
