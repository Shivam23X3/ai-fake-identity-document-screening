"""Step 2 tests: backend foundation — DB, endpoints, validation, providers."""
from __future__ import annotations

import io

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Health & pipeline info
# ---------------------------------------------------------------------------
def test_health(auth_client: TestClient) -> None:
    res = auth_client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["status"] == "healthy"
    assert "request_id" in body


def test_pipeline_info_reports_provider_status(auth_client: TestClient) -> None:
    res = auth_client.get("/pipeline/info")
    assert res.status_code == 200
    payload = res.json()["pipeline"]
    names = [s["name"] for s in payload["stages"]]
    assert names == [
        "preprocess",
        "ocr",
        "document_validation",
        "tampering_detection",
        "face_verification",
        "risk_assessment",
    ]
    providers = payload["providers"]
    assert set(providers.keys()) == {
        "preprocessing", "ocr", "validation", "tampering", "face", "risk",
    }
    # Steps 4-8: preprocessing, OCR, validation, tampering, face + risk are
    # all real engines now.
    assert providers["preprocessing"]["is_placeholder"] is False
    assert providers["ocr"]["is_placeholder"] is False
    assert providers["validation"]["is_placeholder"] is False
    assert providers["tampering"]["is_placeholder"] is False
    assert providers["face"]["is_placeholder"] is False
    assert providers["risk"]["is_placeholder"] is False


# ---------------------------------------------------------------------------
# Upload endpoint: validation paths
# ---------------------------------------------------------------------------
def test_upload_rejects_bad_extension(auth_client: TestClient) -> None:
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": ("evil.exe", io.BytesIO(b"MZ..."), "application/octet-stream")},
    )
    assert res.status_code == 415
    assert res.json()["success"] is False


def test_upload_rejects_content_mismatch(auth_client: TestClient) -> None:
    # .png extension but text content — magic-byte sniffing must catch it.
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": ("fake.png", io.BytesIO(b"this is not an image"), "image/png")},
    )
    assert res.status_code == 415
    assert "magic-byte" in res.json()["error"]


def test_upload_rejects_bad_doc_type_hint(auth_client: TestClient, png_bytes: bytes) -> None:
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": ("doc.png", io.BytesIO(png_bytes), "image/png")},
        data={"doc_type_hint": "diploma"},
    )
    assert res.status_code == 422


def test_upload_success_creates_run(auth_client: TestClient, png_bytes: bytes) -> None:
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": ("doc.png", io.BytesIO(png_bytes), "image/png")},
        data={"doc_type_hint": "passport"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["run_id"]
    assert body["status"] == "uploaded"
    assert body["file"]["detected_type"] == "png"
    assert len(body["file"]["sha256"]) == 64
    assert body["disclaimer"].startswith("AI-assisted")


# ---------------------------------------------------------------------------
# Analyze / detail / history lifecycle
# ---------------------------------------------------------------------------
def _upload(auth_client: TestClient, png_bytes: bytes) -> str:
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": ("doc.png", io.BytesIO(png_bytes), "image/png")},
        data={"doc_type_hint": "passport"},
    )
    return res.json()["run_id"]


def test_analyze_returns_all_stages_and_persists(auth_client: TestClient, png_bytes: bytes) -> None:
    run_id = _upload(auth_client, png_bytes)

    res = auth_client.post(
        "/api/screening/analyze",
        json={"run_id": run_id, "doc_type_hint": "passport"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "pending_review"
    assert body["human_review_required"] is True
    stages = body["stages"]
    assert [s["stage"] for s in stages] == [
        "preprocess",
        "ocr",
        "document_validation",
        "tampering_detection",
        "face_verification",
        "risk_assessment",
    ]
    # Steps 4-8: preprocess, ocr, validation, tampering, face + risk all run
    # real engines (status ok on a valid image). OCR on an uninformative
    # image finds no fields ⇒ routed to review; validation flags missing
    # fields ⇒ review; face without a probe image is INCONCLUSIVE ⇒ review;
    # the risk band reflects the degraded upstream signals ⇒ review.
    by_name = {s["stage"]: s for s in stages}
    assert by_name["preprocess"]["status"] == "ok"
    assert by_name["ocr"]["status"] == "ok"
    assert by_name["ocr"]["human_review_required"] is True  # nothing extractable
    assert by_name["document_validation"]["status"] == "ok"
    assert by_name["document_validation"]["human_review_required"] is True
    assert by_name["face_verification"]["status"] == "ok"
    assert by_name["face_verification"]["human_review_required"] is True
    assert by_name["risk_assessment"]["status"] == "ok"
    assert by_name["risk_assessment"]["human_review_required"] is True
    # Step 6: tampering runs real forensics; on an uninformative image the
    # verdict can only be inconclusive/abstain-ish, never a clean pass.
    tampering_stage = by_name["tampering_detection"]
    assert tampering_stage["status"] == "ok"
    assert tampering_stage["human_review_required"] is True

    # Detail endpoint returns the persisted report (idempotent path).
    res2 = auth_client.get(f"/api/screening/{run_id}")
    assert res2.status_code == 200
    detail = res2.json()
    assert detail["status"] == "pending_review"
    assert detail["file_sha256"]
    assert len(detail["report"]["stages"]) == 6


def test_analyze_unknown_run_404(auth_client: TestClient) -> None:
    res = auth_client.post(
        "/api/screening/analyze", json={"run_id": "0" * 8}
    )
    assert res.status_code == 404


def test_history_lists_earlier_runs(auth_client: TestClient, png_bytes: bytes) -> None:
    first = _upload(auth_client, png_bytes)
    res = auth_client.get("/api/screening/history", params={"limit": 5})
    assert res.status_code == 200
    items = res.json()["items"]
    assert any(item["run_id"] == first for item in items)
    assert res.json()["limit"] == 5


def test_history_limit_validation(auth_client: TestClient) -> None:
    res = auth_client.get("/api/screening/history", params={"limit": 0})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Mock registry service
# ---------------------------------------------------------------------------
def test_mock_registry_lookup_and_stamp(auth_client: TestClient) -> None:
    from app.db.base import get_session_factory
    from app.services.registry_service import lookup_document

    with get_session_factory()() as session:
        hit = lookup_document(session, "passport", "MOCK-P1234567")
        miss = lookup_document(session, "passport", "NOT-THERE")

    assert hit is not None and hit["mock"] is True and hit["is_mock_row"] is True
    assert miss is None


def test_seeding_is_idempotent(auth_client: TestClient) -> None:
    from app.db.base import get_session_factory
    from app.db.seed_mock_registry import seed_mock_registry

    with get_session_factory()() as session:
        added = seed_mock_registry(session)
    assert added == 0  # already seeded by lifespan
