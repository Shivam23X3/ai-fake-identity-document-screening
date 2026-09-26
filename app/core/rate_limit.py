"""API rate limiting (Step 11) — slowapi, per-client, in-process storage.

Security decisions:

- **Why rate limit**: credential stuffing (login) and resource exhaustion
  (upload/analyze run heavy CV pipelines) are the two cheap DoS/brute-force
  vectors; per-client limits blunt both. In-process memory storage fits the
  single-node prototype; a multi-node deploy would move storage to Redis.
- **Proxy-aware client key**: in the Docker demo every request arrives from
  the caddy container, so keying on the raw socket address would put ALL
  visitors into ONE bucket (a single burst could lock the whole demo out).
  ``client_key`` uses the RIGHTMOST X-Forwarded-For hop — the value the
  trusted proxy itself appended — and only when the immediate peer is a
  private-network address (caddy), so a direct client cannot spoof the
  header to rotate buckets. (The leftmost hop is client-controlled and
  would let an attacker trivially bypass per-client limits.)
- **Stricter limits on expensive/dangerous routes**: login 10/min (with
  account lockout behind it), upload 30/min, analyze 20/min — the pipeline
  takes seconds of CPU per run.
- **429s are honest**: the handler returns the standard error envelope with
  Retry-After so well-behaved clients can back off.
"""
from __future__ import annotations

import ipaddress

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.core.config import get_settings


def _is_private_ip(host: str) -> bool:
    """True for loopback/RFC1918/link-local peers (reverse proxies, LAN)."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr in ipaddress.ip_network("172.16.0.0/12")
    )


def client_key(request: Request) -> str:
    """Stable per-client key for rate limiting.

    - Behind a TRUSTED private-network proxy (caddy in the compose demo):
      the RIGHTMOST X-Forwarded-For hop — the one the trusted proxy itself
      appended — is the only hop a direct client cannot forge. A spoofed
      header like ``X-Forwarded-For: 1.2.3.4`` becomes ``"1.2.3.4, <real>"``
      after the proxy appends the socket address, so the rightmost entry is
      still the real client. (Taking the leftmost hop would let any client
      rotate buckets at will and bypass per-client limits entirely.)
    - Direct connections (dev, plain exposed port): the socket address —
      the header is IGNORED so a client cannot spoof fresh buckets.
    """
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and _is_private_ip(peer):
        for hop in reversed([h.strip() for h in forwarded.split(",")]):
            if hop:
                return hop
    return peer


_settings = get_settings()

limiter = Limiter(
    key_func=client_key,
    default_limits=[_settings.rate_limit_default],
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


__all__ = ["client_key", "limiter", "rate_limit_handler"]
