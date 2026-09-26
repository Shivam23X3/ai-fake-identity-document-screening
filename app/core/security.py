"""Security core (Step 11): passwords, JWTs, RBAC, request identity.

Security decisions (each explained):

- **Argon2id** for password hashing (passlib): the current PHC winner,
  memory-hard (GPU/ASIC resistant), no length limit — bcrypt truncates at
  72 bytes. Hashes carry salt + parameters; ``password_needs_rehash`` lets
  us upgrade parameters transparently at login.
- **JWT (HS256, short-lived)** for sessions: stateless, signed with
  ``APP_SECRET_KEY``; tokens embed sub/role/iat/exp/iss/aud + ``jti`` and
  expire in 30 min by default. Short TTL bounds replay risk; revocation is
  handled by a small in-process ``jti`` denylist (single-node deployment —
  Redis would replace this for multi-node).
- **Bearer tokens only** (no cookies): the dashboard is the only client and
  CSRF is structurally impossible with header-borne tokens; ``HttpOnly``
  cookie hardening would add CSRF-token complexity for zero gain here.
- **RBAC deny-by-default**: endpoints declare the roles that may call them;
  anything not allow-listed is 403. Roles: ADMIN / SECURITY_OFFICER /
  REVIEWER with a permission matrix in :data:`PERMISSIONS`.
- **No information leaks**: 401 vs 403 distinguishes authentication from
  authorization; login failures are always the same generic message so
  attackers cannot enumerate usernames; token errors never echo the token.
"""
from __future__ import annotations

import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt as pyjwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.models import User

# ---------------------------------------------------------------------------
# Password hashing — Argon2id
# ---------------------------------------------------------------------------
_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(password, password_hash)
    except Exception:  # noqa: BLE001 - malformed hashes must not crash login
        return False


def password_needs_rehash(password_hash: str) -> bool:
    # CryptContext.needs_rehash exists only in newer passlib; delegate to the
    # underlying scheme handler (present across passlib 1.7.x).
    handler = getattr(_pwd_context, "argon2", None)
    return bool(handler.needs_rehash(password_hash)) if handler else False


def check_password_strength(password: str) -> str | None:
    """Return a rejection reason, or None when the password is acceptable.

    Policy (documented, not arbitrary): >= 12 chars with lower+upper+digit —
    long passphrases beat complex short ones; we do not force symbols.
    """
    if len(password) < 12:
        return "at least 12 characters"
    if not any(c.islower() for c in password):
        return "a lowercase letter"
    if not any(c.isupper() for c in password):
        return "an uppercase letter"
    if not any(c.isdigit() for c in password):
        return "a digit"
    return None


# ---------------------------------------------------------------------------
# JWT access tokens + revocation denylist
# ---------------------------------------------------------------------------
# Dev/test fallback when APP_SECRET_KEY is unset. Deliberately NOT a valid
# stand-in for a real secret: it is a CONSTANT (any dev deployment shares it),
# so prod requires a real APP_SECRET_KEY (config guardrail). 48 bytes keeps
# PyJWT's RFC 7518 minimum-length warning quiet while making the fallback's
# nature obvious by name.
_DEV_FALLBACK_SECRET = "dev-only-insecure-secret-do-not-use-in-prod!"  # noqa: S105

# In-process revocation set (jti). Single-node: this is sufficient; logout
# and password change revoke live tokens immediately. Multi-node would swap
# this for Redis — interface is the two functions below.
_denied_jti: dict[str, float] = {}
_DENYLIST_TTL = 24 * 3600  # sweep window; tokens die naturally in minutes


def _denylist_add(jti: str, exp: float) -> None:
    _denied_jti[jti] = exp
    now = time.time()
    for k in [k for k, v in _denied_jti.items() if v < now]:
        _denied_jti.pop(k, None)


def _denylist_check(jti: str) -> bool:
    return jti in _denied_jti


def create_access_token(user: User, *, force_brief_ttl: bool = False) -> str:
    """Sign a short-lived HS256 access token for ``user``.

    ``force_brief_ttl=True`` is used for the bootstrap admin: its token dies
    in 60 seconds so the one-time bootstrap login cannot become a standing
    credential. The deployer must set a real password right away.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    ttl_minutes = (
        1 if force_brief_ttl else max(1, settings.access_token_expire_minutes)
    )
    payload = {
        "sub": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": uuid.uuid4().hex,
    }
    secret = settings.secret_key or _DEV_FALLBACK_SECRET
    return pyjwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Verify signature + claims. Raises AppError(401) — never leaks the token."""
    settings = get_settings()
    secret = settings.secret_key or _DEV_FALLBACK_SECRET
    try:
        payload = pyjwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
    except pyjwt.ExpiredSignatureError:
        raise AppError("Token expired — sign in again", 401)
    except pyjwt.InvalidTokenError:
        raise AppError("Invalid credentials token", 401)

    if _denylist_check(str(payload.get("jti", ""))):
        raise AppError("Token has been revoked", 401)
    return payload


def revoke_token(token: str) -> None:
    """Best-effort revocation (logout): denylist the token's jti until exp."""
    settings = get_settings()
    secret = settings.secret_key or _DEV_FALLBACK_SECRET
    try:
        payload = pyjwt.decode(
            token, secret, algorithms=["HS256"],
            issuer=settings.jwt_issuer, audience=settings.jwt_audience,
        )
        _denylist_add(str(payload.get("jti", "")), float(payload.get("exp", 0)))
    except Exception:  # noqa: BLE001 - revoking garbage must not crash
        pass


# ---------------------------------------------------------------------------
# Authentication dependency
# ---------------------------------------------------------------------------
async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    """Resolve the caller from the Bearer token; 401 when unauthenticated.

    Also resolves the DB user so role checks see live data (disabled users
    lose access immediately even with a valid token).
    """
    token = _extract_token(request, credentials)
    payload = decode_access_token(token)
    username = str(payload.get("sub") or "")
    if not username:
        raise AppError("Invalid credentials token", 401)

    from app.db.base import get_session_factory

    session = get_session_factory()()
    try:
        user = session.execute(
            _select_user_by_name(username)
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            # Live lookup: a deleted/disabled user cannot ride a valid token.
            raise AppError("Account is inactive or removed", 401)
        request.state.user = user
        return user
    finally:
        session.close()


def _select_user_by_name(username: str):
    from sqlalchemy import select

    return select(User).where(User.username == username)


def _extract_token(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> str:
    if credentials is not None and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    raise AppError("Authentication required", 401)


CurrentUser = Annotated[User, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# RBAC — roles + permission matrix
# ---------------------------------------------------------------------------
ROLES = ("ADMIN", "SECURITY_OFFICER", "REVIEWER")

PERMISSIONS: dict[str, frozenset[str]] = {
    # Who may RUN the screening pipeline / upload documents:
    "screening:run": frozenset({"ADMIN", "SECURITY_OFFICER"}),
    # Who may INSPECT results, reports, history and flagged cases:
    "screening:read": frozenset({"ADMIN", "SECURITY_OFFICER", "REVIEWER"}),
    # Who may READ the audit trail and verify ledger commitments:
    "audit:read": frozenset({"ADMIN", "SECURITY_OFFICER", "REVIEWER"}),
    # Who may WRITE audit records to the ledger:
    "audit:write": frozenset({"ADMIN", "SECURITY_OFFICER"}),
    # Who may manage users, roles and system configuration:
    "admin:manage": frozenset({"ADMIN"}),
}


def has_permission(user: User, permission: str) -> bool:
    return user.role in PERMISSIONS.get(permission, frozenset())


def require_permission(permission: str):
    """Dependency factory: RBAC guard for one permission (403 when denied).

    Deny-by-default: unknown permission ⇒ nobody (even ADMIN) — adding a
    permission requires touching the matrix explicitly.
    """

    async def guard(user: CurrentUser) -> User:
        if not has_permission(user, permission):
            raise AppError(
                "Forbidden — your role does not permit this action", 403
            )
        return user

    return guard


# Convenience annotated guards for route signatures.
require_screening_run = require_permission("screening:run")
require_screening_read = require_permission("screening:read")
require_audit_read = require_permission("audit:read")
require_audit_write = require_permission("audit:write")
require_admin = require_permission("admin:manage")


def generate_api_secret() -> str:
    """Cryptographically random secret (helper for key rotation docs)."""
    return secrets.token_urlsafe(48)


__all__ = [
    "PERMISSIONS",
    "ROLES",
    "check_password_strength",
    "create_access_token",
    "decode_access_token",
    "generate_api_secret",
    "get_current_user",
    "has_permission",
    "hash_password",
    "password_needs_rehash",
    "require_admin",
    "require_audit_read",
    "require_audit_write",
    "require_permission",
    "require_screening_read",
    "require_screening_run",
    "revoke_token",
    "verify_password",
]
