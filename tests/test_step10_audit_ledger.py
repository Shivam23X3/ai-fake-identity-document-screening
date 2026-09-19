"""Step 10 tests: blockchain/audit logging layer (canonical records + ledger).

Layers covered:
- Canonical audit record: exact spec fields, no PII/images/biometrics.
- PII guard: forbidden keys raise; valid record passes.
- Ledger abstraction: local mock backend append/get/verify; backend selection.
- POST /api/audit/log: 200 shape, 409 pre-analyze, 404 unknown, idempotent.
- GET /api/audit/{screening_id}: record + events + ledger state.
- GET /api/audit/verify/{screening_id}: verified when unchanged; CHANGED when
  the stored report no longer matches the commitment; honest NOT-ON-LEDGER.
- Hash-only honesty: the ledger never receives images/PII (EVM adapter sends
  the hash only; local ledger rejects forbidden keys).
- /api/audit/status diagnostics.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.audit.hashing import sha256_hex  # noqa: E402
from app.services.audit.ledger import (  # noqa: E402
    build_audit_record,
    get_local_ledger,
    validate_no_pii,
)


# ---------------------------------------------------------------------------
# Canonical audit record
# ---------------------------------------------------------------------------
class TestAuditRecord:
    def test_record_has_spec_fields(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes)
        record = build_audit_record(run["report"])
        assert set(record) == {
            "screening_id", "timestamp", "result_hash", "risk_score",
            "validation_status", "tampering_status", "face_match_status",
            "system_id",
        }

    def test_result_hash_commits_to_report(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes)
        record = build_audit_record(run["report"])
        assert record["result_hash"] == sha256_hex(run["report"])
        # Deterministic: rebuilding gives the same hash.
        assert build_audit_record(run["report"])["result_hash"] == record["result_hash"]

    def test_status_fields_reflect_pipeline(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes)
        record = build_audit_record(run["report"])
        assert record["validation_status"] in {"valid", "invalid", "unknown"}
        assert record["tampering_status"] in {
            "no_obvious_manipulation", "suspicious", "likely_manipulated",
            "inconclusive", "unknown",
        }
        assert record["face_match_status"] in {"MATCH", "NO_MATCH", "INCONCLUSIVE", "unknown"}
        assert isinstance(record["risk_score"], (int, float)) or record["risk_score"] is None
        assert "v" in record["system_id"]  # version identifier present

    def test_record_carries_no_pii(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes)
        record = build_audit_record(run["report"])
        blob = str(sorted(record.items())).lower()
        for forbidden in ("smith", "alice", "l898902c3", "1974-08-12", "utopia",
                          "original.png", "p<uto"):
            assert forbidden not in blob, f"PII leaked into audit record: {forbidden}"


# ---------------------------------------------------------------------------
# PII guard (defense in depth)
# ---------------------------------------------------------------------------
class TestPIIGuard:
    def test_forbidden_keys_raise(self) -> None:
        with pytest.raises(ValueError):
            validate_no_pii({"screening_id": "x", "portrait": b"..."})
        with pytest.raises(ValueError):
            validate_no_pii({"full_name": "SMITH ALICE"})
        with pytest.raises(ValueError):
            validate_no_pii({"embeddings": [0.1, 0.2]})

    def test_valid_record_passes(self) -> None:
        validate_no_pii({
            "screening_id": "abc", "result_hash": "ab" * 32,
            "risk_score": 10, "validation_status": "valid",
        })


# ---------------------------------------------------------------------------
# Ledger abstraction — local mock backend
# ---------------------------------------------------------------------------
class TestLocalLedger:
    def test_append_get_verify_roundtrip(self) -> None:
        ledger = get_local_ledger()
        record = {
            "screening_id": "feedface" * 8,
            "result_hash": "cd" * 32,
            "risk_score": 12,
            "validation_status": "valid",
            "tampering_status": "inconclusive",
            "face_match_status": "INCONCLUSIVE",
        }
        entry = ledger.append(record)
        assert entry["ledger_status"] == "logged"
        assert entry["sequence"] >= 1
        got = ledger.get("feedface" * 8)
        assert got is not None and got["result_hash"] == "cd" * 32
        v = ledger.verify("feedface" * 8, "cd" * 32)
        assert v["verified"] is True and v["on_ledger"] is True

    def test_verify_detects_mutation(self) -> None:
        ledger = get_local_ledger()
        record = {"screening_id": "abababab" * 8, "result_hash": "ef" * 32}
        ledger.append(record)
        v = ledger.verify("abababab" * 8, "99" * 32)
        assert v["verified"] is False and v["reason"] == "hash_mismatch"

    def test_verify_unknown_screening_is_honest(self) -> None:
        v = get_local_ledger().verify("12345678" * 8, "aa" * 32)
        assert v["verified"] is False
        assert v["on_ledger"] is False and v["reason"] == "no_ledger_entry"

    def test_local_ledger_rejects_pii(self) -> None:
        with pytest.raises(ValueError):
            get_local_ledger().append({"screening_id": "x", "image": b"png"})

    def test_status_discloses_mock_nature(self) -> None:
        status = get_local_ledger().status()
        assert status["implemented"] is True
        assert "NOT durable" in status["note"]


# ---------------------------------------------------------------------------
# POST /api/audit/log
# ---------------------------------------------------------------------------
class TestAuditLogEndpoint:
    def test_log_returns_canonical_record(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes)
        res = auth_client.post("/api/audit/log", json={"run_id": run["run_id"]})
        assert res.status_code == 200
        body = res.json()
        assert body["logged"] is True
        assert body["audit_record"]["screening_id"] == run["run_id"]
        assert body["ledger_backend"] in {"local", "evm"}
        assert body["ledger"]["ledger_status"] in {"logged", "pending", "anchored"}
        # Hash-only promise is stated on the API surface.
        assert "never leave the local database" in body["note"]

    def test_log_is_idempotent_shape(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes)
        b1 = auth_client.post("/api/audit/log", json={"run_id": run["run_id"]}).json()
        b2 = auth_client.post("/api/audit/log", json={"run_id": run["run_id"]}).json()
        assert b1["audit_record"]["result_hash"] == b2["audit_record"]["result_hash"]

    def test_log_409_before_analyze(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, png_bytes)
        res = auth_client.post("/api/audit/log", json={"run_id": run["run_id"]})
        assert res.status_code == 409
        assert "analyze" in res.json()["error"].lower()

    def test_log_422_missing_run_id(self, auth_client) -> None:
        assert auth_client.post("/api/audit/log", json={}).status_code == 422

    def test_log_404_unknown(self, auth_client) -> None:
        assert auth_client.post("/api/audit/log", json={"run_id": "deadbeef" * 8}).status_code == 404

    def test_log_writes_local_event(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes)
        auth_client.post("/api/audit/log", json={"run_id": run["run_id"]})
        events = auth_client.get(f"/api/screening/{run['run_id']}/audit").json()["events"]
        assert any(e["event_type"] == "audit_logged" for e in events)


# ---------------------------------------------------------------------------
# GET /api/audit/{screening_id}
# ---------------------------------------------------------------------------
class TestAuditGetEndpoint:
    def test_get_returns_record_and_events(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes)
        auth_client.post("/api/audit/log", json={"run_id": run["run_id"]})
        res = auth_client.get(f"/api/audit/{run['run_id']}")
        assert res.status_code == 200
        body = res.json()
        assert body["audit_record"]["screening_id"] == run["run_id"]
        assert body["has_audit_log"] is True
        assert body["screening_anchor"]["payload_sha256"]
        assert {e["event_type"] for e in body["events"]} >= {
            "screening_completed", "audit_logged",
        }

    def test_get_409_before_analyze(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, png_bytes)
        assert auth_client.get(f"/api/audit/{run['run_id']}").status_code == 409

    def test_get_404_unknown(self, auth_client) -> None:
        assert auth_client.get("/api/audit/deadbeef00").status_code == 404


# ---------------------------------------------------------------------------
# GET /api/audit/verify/{screening_id} — the tamper-evidence core
# ---------------------------------------------------------------------------
class TestAuditVerifyEndpoint:
    @staticmethod
    def _force_local_ledger(monkeypatch) -> None:
        """Pin the local mock backend so tests are deterministic whether or
        not the dev EVM chain happens to be running (with the chain up,
        analyze auto-anchors and the EVM backend would already be populated)."""
        from app.services.audit import ledger as ledger_mod

        monkeypatch.setattr(
            ledger_mod,
            "get_ledger_backend",
            lambda: ("local", ledger_mod.get_local_ledger()),
        )

    def test_verify_unchanged_after_log(self, auth_client, png_bytes: bytes, monkeypatch) -> None:
        self._force_local_ledger(monkeypatch)
        run = _upload_and_analyze(auth_client, png_bytes)
        auth_client.post("/api/audit/log", json={"run_id": run["run_id"]})
        res = auth_client.get(f"/api/audit/verify/{run['run_id']}")
        assert res.status_code == 200
        body = res.json()
        assert body["verified"] is True
        assert body["record_changed"] is False
        assert "UNCHANGED" in body["conclusion"]
        assert body["db_hash_match"] is True
        assert body["recomputed_result_hash"] == body["db_committed_hash"]

    def test_verify_detects_report_tampering(self, auth_client, png_bytes: bytes, monkeypatch) -> None:
        self._force_local_ledger(monkeypatch)
        run = _upload_and_analyze(auth_client, png_bytes)
        auth_client.post("/api/audit/log", json={"run_id": run["run_id"]})
        _mutate_report(run["run_id"], risk_assessment_risk_score=99)
        body = auth_client.get(f"/api/audit/verify/{run['run_id']}").json()
        assert body["verified"] is False
        assert body["record_changed"] is True
        assert "CHANGED" in body["conclusion"]
        assert body["recomputed_result_hash"] != body["db_committed_hash"]

    def test_verify_not_on_ledger_when_never_logged(self, auth_client, png_bytes: bytes, monkeypatch) -> None:
        self._force_local_ledger(monkeypatch)
        run = _upload_and_analyze(auth_client, png_bytes)
        body = auth_client.get(f"/api/audit/verify/{run['run_id']}").json()
        assert body["verified"] is False
        assert body["record_changed"] is False  # absence is not tampering
        assert body["on_ledger"] is False
        assert "NOT ON LEDGER" in body["conclusion"]

    def test_verify_409_before_analyze(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, png_bytes)
        assert auth_client.get(f"/api/audit/verify/{run['run_id']}").status_code == 409

    def test_verify_404_unknown(self, auth_client) -> None:
        assert auth_client.get("/api/audit/verify/deadbeef00").status_code == 404


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------
class TestAuditStatusEndpoint:
    def test_status_reports_backend_honestly(self, auth_client) -> None:
        res = auth_client.get("/api/audit/status")
        assert res.status_code == 200
        ledger = res.json()["ledger"]
        assert ledger["selected"] in {"local", "evm"}
        assert ledger["engine"] in {"local-mock-ledger", "web3-audit-anchor"}
        if ledger["selected"] == "local":
            assert ledger["note"]  # mock nature disclosed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _upload(auth_client, png_bytes: bytes) -> dict:
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": ("doc.png", png_bytes, "image/png")},
        data={"doc_type_hint": "passport"},
    )
    assert res.status_code == 201, res.text
    return res.json()


def _upload_and_analyze(auth_client, png_bytes: bytes) -> dict:
    """Upload + analyze; return {run_id, report} where report is the stored
    Step-9 consolidated result (exactly what the audit record commits to)."""
    run = _upload(auth_client, png_bytes)
    res = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
    assert res.status_code == 200, res.text
    report = auth_client.get(f"/api/screening/{run['run_id']}").json()["report"]
    return {"run_id": run["run_id"], "report": report}


def _mutate_report(run_id: str, **fields) -> None:
    """Simulate post-hoc tampering with the stored screening result.
    Goes through the crypto layer: report_json is encrypted at rest (Step 11)."""
    import json

    from app.db.base import get_session_factory
    from app.db.models import Screening
    from app.services import screening_service
    from app.services.crypto import encrypt_text, encryption_enabled
    from sqlalchemy import select

    factory = get_session_factory()
    session = factory()
    try:
        row = session.execute(
            select(Screening).where(Screening.run_id == run_id)
        ).scalar_one()
        report = screening_service.load_report(row)
        report["risk_assessment"]["risk_score"] = fields["risk_assessment_risk_score"]
        payload = json.dumps(report)
        row.report_json = encrypt_text(payload) if encryption_enabled() else payload
        row.report_encrypted = encryption_enabled()
        session.commit()
    finally:
        session.close()
