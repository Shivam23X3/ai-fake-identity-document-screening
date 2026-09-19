"""Shared pytest fixtures and test environment setup."""
from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

# --- Test environment: isolate DB + disable file logging -------------------
# Must run BEFORE app modules are imported (get_settings() caches).
_TEST_TMP = Path(tempfile.mkdtemp(prefix="screening-tests-"))
os.environ["APP_DB_URL"] = f"sqlite:///{(_TEST_TMP / 'test.db').as_posix()}"
os.environ["APP_ENV"] = "test"
os.environ["APP_LOG_LEVEL"] = "WARNING"

# Step 11 security test env: deterministic bootstrap admin + encryption key.
os.environ["APP_BOOTSTRAP_ADMIN_USERNAME"] = "testadmin"
os.environ["APP_BOOTSTRAP_ADMIN_PASSWORD"] = "Test#Bootstrap#Passw0rd"
os.environ["APP_ENCRYPTION_KEY"] = "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcy1vay4="  # valid Fernet key (32 raw bytes)
# Generous rate limits: dedicated rate-limit tests use their own env/limiters.
os.environ["APP_RATE_LIMIT_UPLOAD"] = "1000/minute"
os.environ["APP_RATE_LIMIT_ANALYZE"] = "1000/minute"
os.environ["APP_RATE_LIMIT_AUTH"] = "1000/minute"


def make_png_bytes(width: int = 64, height: int = 64, color=(30, 30, 30)) -> bytes:
    """Build a tiny valid PNG in memory (stdlib only, no numpy needed)."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    def row() -> bytes:
        # filter byte 0 + RGB pixels
        return b"\x00" + bytes(color) * width

    raw = b"".join(row() for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """TestClient with lifespan executed (DB created + mock registry seeded
    + bootstrap admin from env credentials)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_headers(client) -> dict:
    """Bearer auth headers for a real ADMIN account.

    The bootstrap admin's token intentionally expires in 60 s (forced
    rotation), so the harness immediately rotates the password via the
    bootstrap token and logs in as a long-lived admin for the session.
    """
    res = client.post(
        "/api/auth/login",
        json={
            "username": os.environ["APP_BOOTSTRAP_ADMIN_USERNAME"],
            "password": os.environ["APP_BOOTSTRAP_ADMIN_PASSWORD"],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    bootstrap_headers = {"Authorization": f"Bearer {body['access_token']}"}
    if body.get("must_change_password"):
        new_pw = "Test#Admin#LongLived#Passw0rd"
        changed = client.post(
            "/api/auth/change-password",
            headers=bootstrap_headers,
            json={
                "old_password": os.environ["APP_BOOTSTRAP_ADMIN_PASSWORD"],
                "new_password": new_pw,
            },
        )
        assert changed.status_code == 200, changed.text
        login_payload = {"username": os.environ["APP_BOOTSTRAP_ADMIN_USERNAME"], "password": new_pw}
    else:
        login_payload = {
            "username": os.environ["APP_BOOTSTRAP_ADMIN_USERNAME"],
            "password": os.environ["APP_BOOTSTRAP_ADMIN_PASSWORD"],
        }
    relogin = client.post("/api/auth/login", json=login_payload)
    assert relogin.status_code == 200, relogin.text
    token = relogin.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth_client(client, admin_headers) -> TestClient:
    """TestClient wrapper that sends admin auth by default.

    Legacy Step-2..10 tests call client.post('/api/screening/upload', ...)
    without headers; with mandatory auth those would all 401. This wrapper
    injects the admin token into every request unless the test overrides
    headers explicitly (for 401/403 tests).
    """
    import re

    original = client.request

    def _request(method, url, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        if not any(k.lower() == "authorization" for k in headers):
            headers.update(admin_headers)
        kwargs["headers"] = headers
        return original(method, url, **kwargs)

    client.request = _request
    yield client
    client.request = original


@pytest.fixture
def png_bytes() -> bytes:
    return make_png_bytes()
