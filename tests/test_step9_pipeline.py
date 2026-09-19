"""Step 9 tests: complete pipeline orchestration + audit trail.

Layers covered:
- Consolidated result contract (spec shape) via the analyze endpoint.
- Unique screening IDs, per-stage timing, per-stage confidence.
- Failure isolation: a crashing module never kills the run.
- Audit trail: audit_events row, canonical report hashing, idempotent hash.
- Chain client honesty: unavailable chain ⇒ anchor 'pending', never faked.
- Consolidated /result endpoint + /audit endpoint.
- Background anchor task wiring.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.audit.hashing import canonical_json, sha256_hex  # noqa: E402


# ---------------------------------------------------------------------------
# Canonical hashing (audit foundation)
# ---------------------------------------------------------------------------
class TestHashing:
    def test_canonical_json_is_deterministic(self) -> None:
        a = {"x": 1, "y": [1, 2], "z": {"b": 2, "a": 1}}
        b = {"z": {"a": 1, "b": 2}, "y": [1, 2], "x": 1}
        assert canonical_json(a) == canonical_json(b)
        assert sha256_hex(a) == sha256_hex(b)

    def test_different_payloads_hash_differently(self) -> None:
        assert sha256_hex({"a": 1}) != sha256_hex({"a": 2})

    def test_hash_is_sha256_hex(self) -> None:
        h = sha256_hex({"run": "x"})
        assert len(h) == 64
        int(h, 16)  # must be hex

    def test_non_serializable_objects_do_not_crash(self) -> None:
        from datetime import date

        assert len(sha256_hex({"d": date(2026, 1, 1)})) == 64


# ---------------------------------------------------------------------------
# Consolidated result via the analyze endpoint (full workflow)
# ---------------------------------------------------------------------------
def _upload(auth_client, name: str, content: bytes, mime: str) -> dict:
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": (name, content, mime)},
        data={"doc_type_hint": "passport"},
    )
    assert res.status_code == 201, res.text
    return res.json()


class TestConsolidatedWorkflow:
    def test_spec_response_shape(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        res = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        assert res.status_code == 200
        result = res.json()["result"]

        # The Step-9 spec contract keys:
        assert set(result) >= {
            "screening_id", "document", "ocr", "validation", "tampering",
            "face_verification", "risk_assessment", "final_status",
            "human_review_required", "processing_time_ms",
        }
        assert result["screening_id"] == run["run_id"]
        assert isinstance(result["processing_time_ms"], int)
        assert result["processing_time_ms"] >= 0
        assert result["human_review_required"] is True
        assert result["final_status"] in {
            "completed", "completed_with_errors", "completed_with_skips",
            "completed_with_degraded_modules", "error",
        }

    def test_unique_screening_ids(self, auth_client, png_bytes: bytes) -> None:
        run1 = _upload(auth_client, "a.png", png_bytes, "image/png")
        run2 = _upload(auth_client, "b.png", png_bytes, "image/png")
        assert run1["run_id"] != run2["run_id"]
        r1 = auth_client.post("/api/screening/analyze", json={"run_id": run1["run_id"]}).json()["result"]
        r2 = auth_client.post("/api/screening/analyze", json={"run_id": run2["run_id"]}).json()["result"]
        assert r1["screening_id"] != r2["screening_id"]

    def test_stage_timing_and_confidence_recorded(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        result = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]}).json()["result"]

        stages = result["stages"]
        assert [s["stage"] for s in stages] == [
            "preprocess", "ocr", "document_validation",
            "tampering_detection", "face_verification", "risk_assessment",
        ]
        for s in stages:
            assert isinstance(s["duration_ms"], int) and s["duration_ms"] >= 0
        # The summary covers every stage (None = engine could not assess).
        assert set(result["confidence_summary"]) == {s["stage"] for s in stages}
        assert result["confidence_summary"]["risk_assessment"] is not None

    def test_intermediate_results_persisted(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        result = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]}).json()["result"]
        # Each module's payload rides in the consolidated result verbatim.
        assert "fields" in result["ocr"]
        assert "checks" in result["validation"] or "is_valid" in result["validation"]
        assert "verdict" in result["tampering"]
        assert "match_status" in result["face_verification"]
        assert "risk_score" in result["risk_assessment"]

    def test_analyze_is_idempotent(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        first = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]}).json()["result"]
        second = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]}).json()["result"]
        assert first["screening_id"] == second["screening_id"]
        assert first["risk_assessment"]["risk_score"] == second["risk_assessment"]["risk_score"]

    def test_module_failure_does_not_crash_the_system(self, auth_client, png_bytes: bytes, monkeypatch) -> None:
        """A crashing face provider must yield an honest result, not a 500."""
        import app.pipelines.stages as stages_mod

        real_get = stages_mod.ai_providers.get_provider

        class _Boom:
            name = "boom-face"

            @staticmethod
            def verify(**kwargs):  # noqa: ANN003 - accepts the stage's kwargs
                raise RuntimeError("face module exploded")

        def _fake(capability: str):
            if capability == "face":
                return _Boom()
            return real_get(capability)

        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        monkeypatch.setattr(stages_mod.ai_providers, "get_provider", _fake)
        res = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        monkeypatch.undo()

        assert res.status_code == 200  # no crash
        result = res.json()["result"]
        face_stage = next(s for s in result["stages"] if s["stage"] == "face_verification")
        assert face_stage["status"] == "error"
        assert "exploded" in (face_stage["error"] or "")
        assert result["final_status"] == "completed_with_errors"
        assert result["human_review_required"] is True
        # The remaining modules still produced results:
        assert result["risk_assessment"]["risk_score"] is not None


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
class TestAuditTrail:
    def test_audit_event_created_with_report_hash(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        result = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]}).json()["result"]

        audit = result.get("audit") or {}
        assert audit.get("payload_sha256")
        assert audit.get("anchor_status") in {"pending", "anchored", "failed"}
        # The hash must commit to the canonical form of the stored report.
        detail = auth_client.get(f"/api/screening/{run['run_id']}").json()["report"]
        assert sha256_hex(detail) == audit["payload_sha256"]

    def test_audit_endpoint_lists_events(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        res = auth_client.get(f"/api/screening/{run['run_id']}/audit")
        assert res.status_code == 200
        body = res.json()
        events = body["events"]
        assert len(events) >= 1
        assert events[0]["event_type"] == "screening_completed"
        assert len(events[0]["payload_sha256"]) == 64
        assert body["chain"]["engine"] == "web3-audit-anchor"
        # No chain node in tests ⇒ honest note, never a fake tx hash.
        assert body["chain"]["implemented"] is False or events[0]["tx_hash"]

    def test_no_fake_transaction_hashes(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        events = auth_client.get(f"/api/screening/{run['run_id']}/audit").json()["events"]
        for e in events:
            if e["anchor_status"] != "anchored":
                assert e["tx_hash"] is None

    def test_hashing_is_idempotent_across_reads(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        first = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]}).json()["result"]
        # Analyze again (idempotent path) → same stored report → same hash.
        second = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]}).json()["result"]
        assert first["audit"]["payload_sha256"] == second["audit"]["payload_sha256"]


# ---------------------------------------------------------------------------
# Chain client honesty (no node in CI)
# ---------------------------------------------------------------------------
class TestChainClient:
    def test_unavailable_chain_reports_pending(self) -> None:
        from app.services.audit.chain_client import AuditAnchorClient

        auth_client = AuditAnchorClient()
        result = auth_client.anchor("deadbeef" * 8, "ab" * 32)
        # Either the dev chain is genuinely running (anchored with a real tx)
        # or it is unreachable and we say so — never a fabricated anchor.
        if result["status"] == "pending":
            assert result["tx_hash"] is None
            assert "not reachable" in (result.get("note") or "")

    def test_status_reports_honestly(self) -> None:
        from app.services.audit.chain_client import AuditAnchorClient

        status = AuditAnchorClient().status()
        assert status["engine"] == "web3-audit-anchor"
        if status["implemented"] is False:
            assert status["note"]


# ---------------------------------------------------------------------------
# Consolidated result endpoint
# ---------------------------------------------------------------------------
class TestResultEndpoint:
    def test_result_endpoint_full_shape(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        res = auth_client.get(f"/api/screening/{run['run_id']}/result")
        assert res.status_code == 200
        result = res.json()
        assert set(result) >= {
            "screening_id", "document", "ocr", "validation", "tampering",
            "face_verification", "risk_assessment", "final_status",
            "human_review_required", "processing_time_ms", "audit",
        }
        assert result["audit"]["payload_sha256"]
        assert "on_chain" in result["audit"]

    def test_result_endpoint_404_unknown(self, auth_client) -> None:
        assert auth_client.get("/api/screening/deadbeef/result").status_code == 404

    def test_result_endpoint_409_before_analyze(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        res = auth_client.get(f"/api/screening/{run['run_id']}/result")
        assert res.status_code == 409


# ---------------------------------------------------------------------------
# Orchestrator unit: idempotency on legacy reports
# ---------------------------------------------------------------------------
class TestOrchestratorUnit:
    def test_legacy_report_is_reprocessed(self, auth_client, png_bytes: bytes) -> None:
        """A pre-Step-9 stored report (no final_status) gets re-run."""
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        # Manually demote the row to a legacy report.
        from app.db.base import get_session_factory

        factory = get_session_factory()
        session = factory()
        try:
            from sqlalchemy import select

            from app.db.models import Screening

            row = session.execute(
                select(Screening).where(Screening.run_id == run["run_id"])
            ).scalar_one()
            # Legacy rows were plaintext; with encryption enabled (Step 11)
            # the flag must match the payload format or reads will fail.
            row.report_json = json.dumps({"status": "uploaded", "stages": []})
            row.report_encrypted = False
            row.status = "pending_review"
            session.commit()
        finally:
            session.close()

        res = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        assert res.status_code == 200
        assert "final_status" in res.json()["result"]
