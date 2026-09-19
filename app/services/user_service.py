"""User management: bootstrap admin, authentication, lockout, RBAC admin.

Security decisions:

- **Bootstrap**: on first startup with an empty users table, an ADMIN is
  created from ``APP_BOOTSTRAP_ADMIN_USERNAME`` +
  ``APP_BOOTSTRAP_ADMIN_PASSWORD`` (environment only — never hardcoded).
  Without a password in the env, seeding is SKIPPED with a loud warning:
  an unpassworded admin account would be worse than none. The bootstrap
  token is forced to expire in 60 s so the initial login cannot become a
  standing credential before the deployer rotates the password.
- **Lockout**: after ``APP_MAX_FAILED_LOGINS`` failed attempts the account
  is locked for ``APP_LOCKOUT_MINUTES`` — brute-force resistance that
  survives across requests (rate limiting alone is per-IP and trivially
  bypassed by distributing attempts).
- **Generic login errors**: wrong username, wrong password and locked
  accounts all return the SAME message — username enumeration via error
  differences is impossible. Timing differences are blurred by hashing a
  dummy hash when the user does not exist (constant-ish work either way).
- **Rehash on login**: when passlib's parameters moved on, the hash is
  transparently upgraded at next successful login.
- **Role changes / deactivation take effect immediately**: tokens carry the
  role claim, but :mod:`app.core.security.get_current_user` re-reads the
  user row; a disabled user is rejected even with a valid token, and role
  changes are reflected because tokens embed the role at issue time and are
  short-lived (30 min max) — for instant effect, password change revokes
  the user's outstanding tokens via the jti denylist.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import (
    check_password_strength,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.db.models import User

logger = logging.getLogger(__name__)

# Generic failure message for EVERY login problem (enumeration resistance).
_LOGIN_FAILED = "Invalid username or password"

_DUMMY_HASH = hash_password("timing-equalizer-dummy-password")  # noqa: S105


def bootstrap_admin(session: Session) -> dict[str, Any]:
    """Create the first ADMIN on first startup (empty users table).

    Returns a summary the caller logs (never includes the password).
    """
    settings = get_settings()
    count = session.execute(select(func.count()).select_from(User)).scalar_one()
    if count > 0:
        return {"created": False, "reason": "users exist"}

    username = (settings.bootstrap_admin_username or "admin").strip() or "admin"
    password = settings.bootstrap_admin_password or ""
    if not password:
        logger.warning(
            "No users exist and APP_BOOTSTRAP_ADMIN_PASSWORD is not set — "
            "ADMIN bootstrap SKIPPED. Nobody can sign in until an admin is "
            "created (set the env var and restart, or use a migration)."
        )
        return {"created": False, "reason": "no bootstrap password in env"}

    user = User(
        username=username,
        password_hash=hash_password(password),
        role="ADMIN",
        is_active=True,
        must_change_password=True,
    )
    session.add(user)
    session.flush()
    # Commit HERE: the lifespan passes a bare session (no get_db teardown
    # commit), and an admin that vanishes with the session is worse than
    # none — the deployer would believe one exists.
    session.commit()
    logger.info(
        "Bootstrap ADMIN '%s' created (password from APP_BOOTSTRAP_ADMIN_PASSWORD; "
        "rotate it after first login).",
        username,
    )
    return {"created": True, "username": username, "role": "ADMIN"}


def _is_locked(user: User) -> bool:
    if user.role == "_never":
        return True  # unreachable; keeps shape stable
    locked_until = getattr(user, "locked_until", None)
    if locked_until is None:
        return False
    if isinstance(locked_until, str):  # SQLite string round-trip tolerance
        try:
            locked_until = datetime.fromisoformat(locked_until)
        except ValueError:
            return False
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > datetime.now(timezone.utc)


def authenticate(session: Session, username: str, password: str) -> dict[str, Any]:
    """Credential check + lockout accounting. Raises AppError(401) on failure.

    Same error, same shape for: unknown user, wrong password, locked
    account, inactive account — no enumeration, no state guessing.
    """
    settings = get_settings()
    username = (username or "").strip()
    user = session.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()

    if user is None:
        verify_password(password, _DUMMY_HASH)  # equalize timing
        raise AppError(_LOGIN_FAILED, 401)

    if _is_locked(user):
        raise AppError(_LOGIN_FAILED, 401)

    if not verify_password(password, user.password_hash):
        failures = int(getattr(user, "failed_login_count", 0) or 0) + 1
        user.failed_login_count = failures
        if failures >= settings.max_failed_logins:
            user.locked_until = datetime.now(timezone.utc) + timedelta(
                minutes=settings.lockout_minutes
            )
            user.failed_login_count = 0
            logger.warning(
                "Account locked after %d failed logins: user_id=%s",
                settings.max_failed_logins,
                user.id,  # id only — never log the username+outcome pairing
            )
        session.commit()
        raise AppError(_LOGIN_FAILED, 401)

    if not user.is_active:
        raise AppError(_LOGIN_FAILED, 401)

    # Success: reset counters, upgrade the hash if parameters moved on.
    user.failed_login_count = 0
    user.locked_until = None
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        logger.info("Password hash upgraded for user_id=%s", user.id)
    session.commit()

    return {"user": user, "bootstrap": bool(user.must_change_password)}


def change_password(
    session: Session, user: User, old_password: str, new_password: str
) -> None:
    """Change own password (strength-checked); caller revokes tokens."""
    # ``user`` is resolved by get_current_user in its own (closed) session —
    # merge it into the REQUEST session or the update silently no-ops.
    user = session.merge(user)
    if not verify_password(old_password, user.password_hash):
        raise AppError("Current password is incorrect", 401)
    reason = check_password_strength(new_password)
    if reason:
        raise AppError(f"New password needs {reason}.", 422)
    user.password_hash = hash_password(new_password)
    # Rotation clears the bootstrap flag: next login gets a normal token.
    user.must_change_password = False
    session.commit()


def admin_set_password(session: Session, user_id: int, new_password: str) -> None:
    """ADMIN resets a password (strength-checked); no old password needed."""
    user = session.get(User, user_id)
    if user is None:
        raise AppError("User not found", 404)
    reason = check_password_strength(new_password)
    if reason:
        raise AppError(f"New password needs {reason}.", 422)
    user.password_hash = hash_password(new_password)
    user.failed_login_count = 0
    user.locked_until = None
    user.must_change_password = False
    session.commit()
    logger.info("Password reset by admin for user_id=%s", user_id)


def create_user(
    session: Session, *, username: str, password: str, role: str
) -> User:
    username = (username or "").strip()
    if len(username) < 3 or len(username) > 64:
        raise AppError("Username must be 3-64 characters", 422)
    if not all(c.isalnum() or c in "._-" for c in username):
        raise AppError("Username may contain letters, digits, dot, dash, underscore", 422)
    if role not in ("ADMIN", "SECURITY_OFFICER", "REVIEWER"):
        raise AppError("role must be one of ADMIN, SECURITY_OFFICER, REVIEWER", 422)
    reason = check_password_strength(password)
    if reason:
        raise AppError(f"Password needs {reason}.", 422)
    exists = session.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()
    if exists is not None:
        # 409 (not 400) so the dashboard can show a distinct message; this
        # leaks only that the name is taken — acceptable for an admin-only
        # user-management surface.
        raise AppError("Username already exists", 409)
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    session.flush()
    logger.info("User created: user_id=%s role=%s", user.id, role)
    return user


def list_users(session: Session) -> list[User]:
    return list(session.execute(select(User).order_by(User.id)).scalars())


def deactivate_user(session: Session, user_id: int, actor: User) -> None:
    """Deactivate (soft-disable) a user. ADMIN cannot deactivate themselves
    — prevents the last-admin lockout footgun."""
    user = session.get(User, user_id)
    if user is None:
        raise AppError("User not found", 404)
    if user.id == actor.id:
        raise AppError("You cannot deactivate your own account", 422)
    user.is_active = False
    session.commit()
    logger.info("User deactivated: user_id=%s", user_id)
