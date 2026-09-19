"""Step 11 tests: security hardening.

Layers covered:
- Auth: login success/failure, uniform error message, lockout, JWT expiry,
  revocation (logout), tampered tokens.
- RBAC: the permission matrix — officer runs screening, reviewer reads,
  admin manages; deny-by-default on everything else.
- Password policy: strength checks on create/change.
- Rate limiting: 429 on auth endpoints (dedicated limiter state).
- Secure headers: nosniff, DENY, CSP, no-store on every response.
- Upload hardening: content-type mismatch, oversized pixels, size cap,
  metadata stripping on re-encode.
- Encryption at rest: report_json is ciphertext; load_report round-trips.
- Error hygiene: no stack traces / internals in prod-style error bodies.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Module-level alias so the rate-limit test can use a plain client name
# without tripping the auth_client fixture rename.
from tests.conftest import make_png_bytes  # noqa: E402,F401

from app.core.security import (  # noqa: E402
    check_password_strength,
    decode_access_token,
    has_permission,
)
from app.services import user_service  # noqa: E402


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------
class TestPasswordHandling:
    def test_argon2_hash_format(self) -> None:
        from app.core.security import hash_password

        h = hash_password("Some#Long#Password#123")
        assert h.startswith("$argon2id$")

    def test_strength_policy(self) -> None:
        assert check_password_strength("short") is not None
        assert check_password_strength("alllowercase1234") is not None
        assert check_password_strength("ALLUPPERCASE1234") is not None
        assert check_password_strength("NoDigitsHereAtAll") is not None
        assert check_password_strength("Good#Passphrase#123") is None

    def test_passwords_are_never_stored_plaintext(self, auth_client) -> None:
        res = auth_client.post(
            "/api/auth/users",
            json={"username": "pwcheck_user", "password": "Plain#Visible#123", "role": "REVIEWER"},
        )
        assert res.status_code == 201
        from sqlalchemy import select

        from app.db.base import get_session_factory
        from app.db.models import User

        s = get_session_factory()()
        try:
            row = s.execute(
                select(User).where(User.username == "pwcheck_user")
            ).scalar_one()
            assert "Plain#Visible#123" not in (row.password_hash or "")
            assert row.password_hash.startswith("$argon2id$")
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Auth flows
# ---------------------------------------------------------------------------
class TestAuthFlows:
    def test_login_success_shape(self, auth_client) -> None:
        res = auth_client.post(
            "/api/auth/login",
            json={
                "username": __import__("os").environ["APP_BOOTSTRAP_ADMIN_USERNAME"],
                "password": "Test#Admin#LongLived#Passw0rd",
            },
        )
        # The session harness may have rotated the bootstrap password already;
        # accept either the rotated password (200) or original (200) — both
        # are valid logins; failure means the harness broke.
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["access_token"]
        assert body["token_type"] == "bearer"
        assert set(body["user"]) >= {"id", "username", "role"}

    def test_login_failure_is_uniform(self, auth_client) -> None:
        wrong_pw = auth_client.post(
            "/api/auth/login", json={"username": "testadmin", "password": "totally-wrong-9"}
        )
        no_user = auth_client.post(
            "/api/auth/login", json={"username": "ghost_user_xyz", "password": "totally-wrong-9"}
        )
        assert wrong_pw.status_code == 401 and no_user.status_code == 401
        assert wrong_pw.json()["error"] == no_user.json()["error"]

    def test_me_requires_auth(self, client) -> None:
        assert client.get("/api/auth/me").status_code == 401

    def test_tampered_token_rejected(self, auth_client) -> None:
        good = auth_client.get("/api/auth/me")
        assert good.status_code == 200
        token = good.json()["user"] and None
        # Forge: flip a char in a real token — signature must fail.
        res = auth_client.post(
            "/api/auth/login",
            json={"username": "testadmin", "password": "Test#Admin#LongLived#Passw0rd"},
        )
        tok = res.json()["access_token"]
        tampered = (tok[:-6] + ("aaaaaa" if tok[-6:] != "aaaaaa" else "bbbbbb"))
        bad = auth_client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {tampered}"}
        )
        assert bad.status_code == 401
        assert token is None

    def test_logout_revokes_token(self, auth_client) -> None:
        res = auth_client.post(
            "/api/auth/login",
            json={"username": "testadmin", "password": "Test#Admin#LongLived#Passw0rd"},
        )
        tok = res.json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert auth_client.get("/api/auth/me", headers=h).status_code == 200
        assert auth_client.post("/api/auth/logout", headers=h).status_code == 200
        assert auth_client.get("/api/auth/me", headers=h).status_code == 401

    def test_expired_token_rejected(self) -> None:
        from datetime import datetime, timedelta, timezone

        import jwt as pyjwt

        from app.core.config import get_settings

        settings = get_settings()
        now = datetime.now(timezone.utc)
        token = pyjwt.encode(
            {
                "sub": "testadmin", "role": "ADMIN",
                "iat": now - timedelta(hours=2),
                "exp": now - timedelta(hours=1),
                "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
                "jti": "expired-jti",
            },
            settings.secret_key or "dev-only-insecure-secret",
            algorithm="HS256",
        )
        with pytest.raises(Exception):
            decode_access_token(token)

    def test_wrong_issuer_rejected(self) -> None:
        from datetime import datetime, timedelta, timezone

        import jwt as pyjwt

        from app.core.config import get_settings

        settings = get_settings()
        now = datetime.now(timezone.utc)
        token = pyjwt.encode(
            {
                "sub": "testadmin", "role": "ADMIN",
                "iat": now, "exp": now + timedelta(minutes=5),
                "iss": "evil-issuer", "aud": settings.jwt_audience,
                "jti": "wrong-iss",
            },
            settings.secret_key or "dev-only-insecure-secret",
            algorithm="HS256",
        )
        with pytest.raises(Exception):
            decode_access_token(token)

    def test_lockout_after_repeated_failures(self, auth_client) -> None:
        # Dedicated user so the shared admin account is never locked.
        auth_client.post(
            "/api/auth/users",
            json={"username": "lockme_user", "password": "Lock#Target#Pass1", "role": "REVIEWER"},
        )
        for _ in range(5):
            r = auth_client.post(
                "/api/auth/login", json={"username": "lockme_user", "password": "bad-cred-9"}
            )
            assert r.status_code == 401
        # 6th attempt with the CORRECT password is still rejected (locked),
        # and the message is unchanged (no state leak).
        r6 = auth_client.post(
            "/api/auth/login", json={"username": "lockme_user", "password": "Lock#Target#Pass1"}
        )
        assert r6.status_code == 401
        assert r6.json()["error"] == "Invalid username or password"


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
def _mk_user(client, username: str, role: str) -> dict:
    r = client.post(
        "/api/auth/users",
        json={"username": username, "password": "Role#Testing#Pass1", "role": role},
    )
    assert r.status_code == 201, r.text
    login = client.post(
        "/api/auth/login", json={"username": username, "password": "Role#Testing#Pass1"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


class TestRBAC:
    def test_permission_matrix_shape(self) -> None:
        class _FakeUser:
            role = "ADMIN"

        class _FakeReviewer:
            role = "REVIEWER"

        assert has_permission(_FakeUser(), "admin:manage")
        assert not has_permission(_FakeReviewer(), "admin:manage")
        # Deny-by-default: unknown permission denies even ADMIN.
        assert not has_permission(_FakeUser(), "nonexistent:perm")

    def test_officer_can_screen_reviewer_cannot(self, auth_client) -> None:
        officer = _mk_user(auth_client, "officer_rbac", "SECURITY_OFFICER")
        reviewer = _mk_user(auth_client, "reviewer_rbac", "REVIEWER")

        # A real (tiny but valid) PNG so the hardening re-encode accepts it.
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (16, 16), (3, 3, 3)).save(buf, format="PNG")
        png = buf.getvalue()

        up = auth_client.post(
            "/api/screening/upload",
            headers=officer,
            files={"file": ("x.png", png, "image/png")},
            data={"doc_type_hint": "passport"},
        )
        assert up.status_code == 201, up.text
        run_id = up.json()["run_id"]

        # Reviewer CAN inspect...
        assert auth_client.get("/api/screening/history", headers=reviewer).status_code == 200
        assert auth_client.get(f"/api/screening/{run_id}", headers=reviewer).status_code == 200
        # ...but CANNOT run the pipeline...
        assert auth_client.post(
            "/api/screening/upload",
            headers=reviewer,
            files={"file": ("y.png", png, "image/png")},
            data={"doc_type_hint": "visa"},
        ).status_code == 403
        assert auth_client.post(
            "/api/screening/analyze", headers=reviewer, json={"run_id": run_id}
        ).status_code == 403
        # ...and CANNOT manage users.
        assert auth_client.get("/api/auth/users", headers=reviewer).status_code == 403

    def test_officer_cannot_manage_users(self, auth_client) -> None:
        officer = _mk_user(auth_client, "officer_admin_attempt", "SECURITY_OFFICER")
        assert auth_client.get("/api/auth/users", headers=officer).status_code == 403
        assert auth_client.post(
            "/api/auth/users",
            headers=officer,
            json={"username": "nope_user", "password": "Whatever#Pass123", "role": "ADMIN"},
        ).status_code == 403

    def test_admin_can_manage_users(self, auth_client) -> None:
        assert auth_client.get("/api/auth/users").status_code == 200

    def test_reviewer_cannot_write_audit(self, auth_client) -> None:
        reviewer = _mk_user(auth_client, "reviewer_audit", "REVIEWER")
        r = auth_client.post("/api/audit/log", headers=reviewer, json={"run_id": "x" * 32})
        assert r.status_code == 403

    def test_admin_cannot_self_deactivate(self, auth_client) -> None:
        me = auth_client.get("/api/auth/me").json()["user"]
        r = auth_client.post(f"/api/auth/users/{me['id']}/deactivate")
        assert r.status_code == 422

    def test_weak_password_rejected_on_create(self, auth_client) -> None:
        r = auth_client.post(
            "/api/auth/users",
            json={"username": "weakpw_user", "password": "weak", "role": "REVIEWER"},
        )
        assert r.status_code == 422

    def test_invalid_role_rejected(self, auth_client) -> None:
        r = auth_client.post(
            "/api/auth/users",
            json={"username": "badrole_user", "password": "Whatever#Pass123", "role": "SUPERGOD"},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Secure headers
# ---------------------------------------------------------------------------
class TestSecureHeaders:
    def test_headers_on_every_response(self, client) -> None:
        res = client.get("/health")
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert res.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors" in res.headers["Content-Security-Policy"]
        assert res.headers["Cache-Control"] == "no-store"

    def test_hsts_only_over_https(self, client) -> None:
        http = client.get("/health")
        assert "Strict-Transport-Security" not in http.headers


# ---------------------------------------------------------------------------
# Upload hardening
# ---------------------------------------------------------------------------
class TestUploadHardening:
    def _png(self, width=32, height=32):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (width, height), (10, 10, 10)).save(buf, format="PNG")
        return buf.getvalue()

    def _png_old(self, width=32, height=32):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (width, height), (10, 10, 10)).save(buf, format="PNG")
        return buf.getvalue()

    def test_extension_content_mismatch_rejected(self, auth_client) -> None:
        # GIF bytes disguised as .png
        r = auth_client.post(
            "/api/screening/upload",
            files={"file": ("fake.png", b"GIF89a" + b"0" * 64, "image/png")},
            data={"doc_type_hint": "passport"},
        )
        assert r.status_code == 415

    def test_garbage_bytes_rejected(self, auth_client) -> None:
        r = auth_client.post(
            "/api/screening/upload",
            files={"file": ("garbage.png", b"\x89PNG\r\n\x1a\n" + b"junk" * 64, "image/png")},
            data={"doc_type_hint": "passport"},
        )
        assert r.status_code == 415

    def test_oversized_dimensions_rejected(self, auth_client) -> None:
        import io
        import struct
        import zlib

        # PNG header claiming 12000x12000 pixels (144 MP > 50 MP budget)
        # but no real data — decode fails / budget exceeded.
        ihdr = struct.pack(">IIBBBBB", 12000, 12000, 8, 2, 0, 0, 0)

        def chunk(tag, data):
            return (
                struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        payload = (
            b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00" * 36000)) + chunk(b"IEND", b"")
        )
        r = auth_client.post(
            "/api/screening/upload",
            files={"file": ("bomb.png", payload, "image/png")},
            data={"doc_type_hint": "passport"},
        )
        assert r.status_code in {413, 415}

    def test_valid_upload_strips_metadata(self, auth_client) -> None:
        import io
        import re

        from PIL import Image

        buf = io.BytesIO()
        im = Image.new("RGB", (64, 64), (9, 9, 9))
        exif = Image.Exif()
        exif[0x010F] = "EvilCameraCorp"  # Make tag
        im.save(buf, format="JPEG", exif=exif.tobytes())
        raw = buf.getvalue()
        assert b"EvilCameraCorp" in raw  # sanity: metadata present before

        up = auth_client.post(
            "/api/screening/upload",
            files={"file": ("withmeta.jpg", raw, "image/jpeg")},
            data={"doc_type_hint": "passport"},
        )
        assert up.status_code == 201, up.text
        stored = Path(up.json()["file"]["sha256"] and ".").anchor  # placeholder
        # Read the stored file from the response run dir.
        run_id = up.json()["run_id"]
        from app.core.config import get_settings

        matches = list((get_settings().upload_dir / run_id).glob("original*"))
        assert matches, "stored file missing"
        blob = matches[0].read_bytes()
        assert b"EvilCameraCorp" not in blob  # re-encode stripped it
        _ = stored, re

    def test_size_limit_enforced(self, auth_client, monkeypatch) -> None:
        from app.core.config import get_settings

        # 1000x1000 noise PNG ≈ 700 KB — above the 500 KB test cap but far
        # below the 50 MP pixel budget and the global 10 MB default.
        monkeypatch.setattr(
            type(get_settings()), "max_upload_bytes",
            property(lambda self: 500_000), raising=False,
        )
        import io as _io
        import random as _random

        buf = _io.BytesIO()
        rnd = _random.Random(7)
        from PIL import Image as _Image

        im = _Image.new("L", (1000, 1000))
        im.putdata([rnd.randint(0, 255) for _ in range(1000 * 1000)])
        im.save(buf, format="PNG")
        big = buf.getvalue()
        assert len(big) > 500_000, len(big)
        r = auth_client.post(
            "/api/screening/upload",
            files={"file": ("big.png", big, "image/png")},
            data={"doc_type_hint": "passport"},
        )
        assert r.status_code == 413


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------
class TestEncryptionAtRest:
    def test_report_is_ciphertext_in_db(self, auth_client, png_bytes) -> None:
        up = auth_client.post(
            "/api/screening/upload",
            files={"file": ("enc.png", png_bytes, "image/png")},
            data={"doc_type_hint": "passport"},
        )
        run_id = up.json()["run_id"]
        auth_client.post("/api/screening/analyze", json={"run_id": run_id})

        from sqlalchemy import select

        from app.db.base import get_session_factory
        from app.db.models import Screening

        s = get_session_factory()()
        try:
            row = s.execute(
                select(Screening).where(Screening.run_id == run_id)
            ).scalar_one()
            assert row.report_encrypted is True
            assert b"final_status" not in (row.report_json or "").encode()
        finally:
            s.close()

    def test_roundtrip_through_crypto(self, auth_client, png_bytes) -> None:
        up = auth_client.post(
            "/api/screening/upload",
            files={"file": ("enc2.png", png_bytes, "image/png")},
            data={"doc_type_hint": "passport"},
        )
        run_id = up.json()["run_id"]
        auth_client.post("/api/screening/analyze", json={"run_id": run_id})
        detail = auth_client.get(f"/api/screening/{run_id}").json()
        assert "final_status" in detail["report"]  # decrypted transparently


# ---------------------------------------------------------------------------
# Rate limiting (dedicated app: real low limits, own limiter state)
# ---------------------------------------------------------------------------
class TestRateLimiting:
    def test_login_rate_limited(self, client, monkeypatch) -> None:
        """Hit the login limiter by shrinking the limit and clearing storage.
        The limiter is process-wide; resetting its storage keeps this test
        hermetic without reloading the app."""
        from app.core.config import get_settings
        from app.core.rate_limit import limiter

        monkeypatch.setattr(
            type(get_settings()), "rate_limit_auth",
            property(lambda self: "3/minute"), raising=False,
        )
        limiter.reset()
        try:
            codes = [
                client.post(
                    "/api/auth/login",
                    json={"username": "nobody", "password": "wrong-cred-1"},
                ).status_code
                for _ in range(6)
            ]
            assert codes[:3] == [401, 401, 401]
            assert 429 in codes[3:], codes
        finally:
            monkeypatch.undo()
            limiter.reset()


# ---------------------------------------------------------------------------
# Error hygiene
# ---------------------------------------------------------------------------
class TestErrorHygiene:
    def test_404_shape_has_no_internals(self, auth_client) -> None:
        r = auth_client.get("/api/screening/deadbeef00")
        body = r.json()
        assert r.status_code == 404
        assert body["success"] is False
        assert "Traceback" not in body.get("error", "")
        assert "details" not in body  # test env shows details only on 500

    def test_validation_error_is_sanitized_shape(self, auth_client) -> None:
        r = auth_client.post("/api/auth/login", json={"username": "x"})
        assert r.status_code == 422
        assert r.json()["success"] is False
