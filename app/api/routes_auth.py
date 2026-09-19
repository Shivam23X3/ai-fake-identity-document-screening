"""Authentication + user-management endpoints (Step 11).

    POST /api/auth/login            → issue a JWT (rate-limited)
    GET  /api/auth/me               → caller identity + role + permissions
    POST /api/auth/logout           → revoke the presented token (jti denylist)
    POST /api/auth/change-password  → strength-checked self-service change
    GET  /api/auth/users            → list users            (ADMIN)
    POST /api/auth/users            → create user + role   (ADMIN)
    POST /api/auth/users/{id}/password → reset password    (ADMIN)
    POST /api/auth/users/{id}/deactivate → disable account (ADMIN)

Security decisions:
- Login failures return ONE generic message (enumeration resistance) and
  the endpoint is rate-limited hardest of all (credential-stuffing target).
- /me returns the permission matrix so the frontend can hide controls, but
  authorization is ALWAYS re-checked server-side — hiding UI is UX, not
  security.
- Logout revokes the presented token server-side; short TTL bounds any
  residual window.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.responses import DISCLAIMER, ok
from app.core.security import PERMISSIONS, CurrentUser, create_access_token, require_admin, revoke_token
from app.db.base import get_db
from app.db.models import User
from app.services import user_service

router = APIRouter(prefix="/api/auth", tags=["auth"])

DbDep = Annotated[Session, Depends(get_db)]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: str = Field(default="REVIEWER")


def _public_user(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.post("/login")
@limiter.limit(lambda: get_settings().rate_limit_auth)
def login(body: LoginRequest, request: Request, db: DbDep = None):
    """Issue a short-lived JWT. Uniform 401 for every failure mode."""
    result = user_service.authenticate(db, body.username, body.password)
    user = result["user"]
    # The bootstrap admin gets a 60-second token: enough to rotate the
    # password immediately, useless as a standing credential.
    token = create_access_token(user, force_brief_ttl=result.get("bootstrap", False))
    return ok(
        {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": (
                60
                if result.get("bootstrap")
                else get_settings().access_token_expire_minutes * 60
            ),
            "user": _public_user(user),
            "permissions": [
                p for p, roles in PERMISSIONS.items() if user.role in roles
            ],
            "must_change_password": bool(result.get("bootstrap")),
            "disclaimer": DISCLAIMER,
        }
    )


@router.get("/me")
def me(user: CurrentUser):
    """Caller identity. Role is re-read live from the DB (not the token)."""
    from app.core.security import has_permission

    return ok(
        {
            "user": _public_user(user),
            "permissions": {p: has_permission(user, p) for p in PERMISSIONS},
            "disclaimer": DISCLAIMER,
        }
    )


@router.post("/logout")
def logout(request: Request, user: CurrentUser):
    """Revoke the presented token (denylist its jti until natural expiry)."""
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        revoke_token(header[7:].strip())
    return ok({"logged_out": True, "disclaimer": DISCLAIMER})


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, user: CurrentUser, db: DbDep = None):
    """Strength-checked self-service password change; revokes other tokens
    by leaving them to expire (30-min bound) — denylist is per-jti and the
    user's other sessions are server-side unknown by design."""
    user_service.change_password(db, user, body.old_password, body.new_password)
    return ok({"changed": True, "disclaimer": DISCLAIMER})


@router.get("/users")
def list_users(admin: User = Depends(require_admin), db: DbDep = None):
    users = user_service.list_users(db)
    return ok({"users": [_public_user(u) for u in users], "disclaimer": DISCLAIMER})


@router.post("/users", status_code=201)
def create_user(
    body: CreateUserRequest, admin: User = Depends(require_admin), db: DbDep = None
):
    user = user_service.create_user(
        db, username=body.username, password=body.password, role=body.role
    )
    return ok(
        {"user": _public_user(user), "disclaimer": DISCLAIMER}, status_code=201
    )


@router.post("/users/{user_id}/password")
def admin_reset_password(
    user_id: int,
    body: ChangePasswordRequest,
    admin: User = Depends(require_admin),
    db: DbDep = None,
):
    """ADMIN password reset: requires new_password only (old_password is
    ignored and may be omitted by the admin; the DTO is reused for shape)."""
    user_service.admin_set_password(db, user_id, body.new_password)
    return ok({"reset": True, "disclaimer": DISCLAIMER})


@router.post("/users/{user_id}/deactivate")
def admin_deactivate_user(
    user_id: int, admin: User = Depends(require_admin), db: DbDep = None
):
    user_service.deactivate_user(db, user_id, admin)
    return ok({"deactivated": True, "disclaimer": DISCLAIMER})


# Roles are exported for the frontend user-management dialog.
@router.get("/roles")
def list_roles():
    return ok({"roles": list(ROLES), "permissions": PERMISSIONS, "disclaimer": DISCLAIMER})
