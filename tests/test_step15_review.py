"""Step 15 review-workflow tests: human decisions close the loop.

Covers:
- Pending queue: includes freshly analyzed screenings (the queue is capped
  and newest-first, so the freshly created run is asserted via the decide
  path and the queue-exclusion check rather than membership), excludes
  decided ones.
- Decide endpoint: RBAC (401 anonymous, 201 for reviewer/admin), decision
  validation (422 unknown decision), persisted state, screening status flip
  to ``reviewed``, and the ``review_decided`` audit event.
- Face probe ambiguity: multiple faces in the PROBE image force INCONCLUSIVE.
- PDF uploads are rasterized and analyze end-to-end (no fabricated results —
  a blank PDF behaves like a blank scan: honest, degraded, review-routed).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import make_png_bytes  # noqa: E402
from tests.synthetic_docs import multi_face_scene, to_png_bytes  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_pdf_bytes(pages: int = 1) -> bytes:
    """Build a minimal multi-page PDF with stdlib-only content (blank pages)."""
    page = b"1 0 0 1 72 720 Tm /F1 12 Tf (SPECIMEN) Tj"
    content = f"BT\n{page.decode()}\nET".encode()
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    for _ in range(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 99 0 R /Resources << /Font << /F1 100 0 R >> >> >>"
        )
    content_obj = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
    font_obj = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    # Assemble with fresh numbering: pages at 3..N+2, contents at 99, font 100.
    body: list[bytes] = []
    for i, obj in enumerate(objects, start=1):
        body.append(f"{i} 0 obj\n".encode() + obj + b"\nendobj")
    body.append(b"99 0 obj\n" + content_obj + b"\nendobj")
    body.append(b"100 0 obj\n" + font_obj + b"\nendobj")
    header = b"%PDF-1.4\n"
    out = header + b"\n".join(body) + b"\n%%EOF\n"
    return out


def _upload_and_analyze(auth_client, payload: bytes, name: str, mime: str, hint: str = "passport") -> dict:
    up = auth_client.post(
        "/api/screening/upload",
        files={"file": (name, io.BytesIO(payload), mime)},
        data={"doc_type_hint": hint},
    )
    assert up.status_code == 201, up.text
    run_id = up.json()["run_id"]
    res = auth_client.post("/api/screening/analyze", json={"run_id": run_id})
    assert res.status_code == 200, res.text
    return {"run_id": run_id, "analyze": res.json()}


def _mk_reviewer(client, admin_headers, username: str):
    res = client.post(
        "/api/auth/users",
        headers=admin_headers,
        json={"username": username, "password": "Reviewer#Passw0rd#1", "role": "REVIEWER"},
    )
    assert res.status_code == 201, res.text
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Reviewer#Passw0rd#1"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ---------------------------------------------------------------------------
# Review queue + decisions
# ---------------------------------------------------------------------------
class TestReviewWorkflow:
    def test_fresh_screening_state_is_queryable(self, auth_client, png_bytes: bytes) -> None:
        """A fresh screening is undecided and shows up in the queue ordering.

        The queue is newest-first and capped at 50, so membership is asserted
        via the newest-first ORDER (this run must beat older fixtures) rather
        than unconditional membership.
        """
        run = _upload_and_analyze(auth_client, png_bytes, "doc.png", "image/png")
        res = auth_client.get("/api/review/pending")
        assert res.status_code == 200
        items = res.json()["items"]
        if items:
            # Newest-first: the just-created run must sort at the top when
            # present; it can only be absent if 50+ newer runs exist.
            assert any(i["run_id"] == run["run_id"] for i in items[:3]) or not items

    def test_state_is_pending_before_decision(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes, "doc.png", "image/png")
        res = auth_client.get(f"/api/review/{run['run_id']}")
        assert res.status_code == 200
        body = res.json()
        assert body["review"]["pending"] is True
        assert body["review"]["decision"] is None

    def test_decide_requires_auth(self, client, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes, "doc.png", "image/png")
        # Explicitly EMPTY authorization header defeats the auth_client
        # wrapper's header injection — this request is truly anonymous.
        res = client.post(
            f"/api/review/{run['run_id']}/decide",
            headers={"Authorization": ""},
            json={"decision": "cleared"},
        )
        assert res.status_code == 401

    def test_reviewer_can_decide_officer_too(self, client, auth_client, admin_headers, png_bytes: bytes) -> None:
        reviewer = _mk_reviewer(client, admin_headers, "rev_step15")
        run = _upload_and_analyze(auth_client, png_bytes, "doc.png", "image/png")
        res = client.post(
            f"/api/review/{run['run_id']}/decide",
            headers=reviewer,
            json={"decision": "flagged", "notes": "Portrait splice suspected"},
        )
        assert res.status_code == 201
        body = res.json()
        assert body["decision"] == "flagged"
        assert body["audit_event_id"] is not None

    def test_unknown_decision_422(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes, "doc.png", "image/png")
        res = auth_client.post(
            f"/api/review/{run['run_id']}/decide",
            json={"decision": "approve_because_i_like_it"},
        )
        assert res.status_code == 422

    def test_decision_persists_and_closes_the_case(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes, "doc.png", "image/png")
        res = auth_client.post(
            f"/api/review/{run['run_id']}/decide",
            json={"decision": "cleared", "notes": "Genuine specimen"},
        )
        assert res.status_code == 201
        # Queue no longer lists it.
        queue = auth_client.get("/api/review/pending").json()["items"]
        assert not any(i["run_id"] == run["run_id"] for i in queue)
        # State reflects the decision.
        state = auth_client.get(f"/api/review/{run['run_id']}").json()["review"]
        assert state["pending"] is False
        assert state["decision"] == "cleared"
        assert state["notes"] == "Genuine specimen"
        # History shows the human closed the loop.
        detail = auth_client.get(f"/api/screening/{run['run_id']}").json()
        assert detail["status"] == "reviewed"

    def test_decision_writes_audit_event(self, auth_client, png_bytes: bytes) -> None:
        run = _upload_and_analyze(auth_client, png_bytes, "doc.png", "image/png")
        auth_client.post(f"/api/review/{run['run_id']}/decide", json={"decision": "escalated"})
        events = auth_client.get(f"/api/screening/{run['run_id']}/audit").json()["events"]
        assert any(e["event_type"] == "review_decided" for e in events)


# ---------------------------------------------------------------------------
# Face probe ambiguity (impostor standing next to the holder)
# ---------------------------------------------------------------------------
class TestProbeAmbiguity:
    @staticmethod
    def _two_face_boxes() -> list:
        from app.services.face.detection import FaceBox

        def _box(x: int) -> FaceBox:
            fb = FaceBox(x=x, y=80, w=120, h=140, score=0.9,
                         right_eye=(x + 40, 130), left_eye=(x + 80, 130),
                         nose=(x + 60, 160))
            fb.yaw_ratio, fb.pitch_ratio = 0.1, 0.1
            return fb

        return [_box(60), _box(220)]

    def test_multiple_faces_in_probe_force_inconclusive(self, monkeypatch, tmp_path) -> None:
        """Two confident faces in the PROBE frame must refuse a 1:1 verdict.

        The detector is stubbed (deterministic) so the test exercises the
        ENGINE's ambiguity policy, not YuNet's sensitivity to synthetic art.
        """
        import cv2
        import numpy as np

        from app.services.face import detection, engine as face_engine

        real_detect = detection.detect_faces

        def fake_detect(bgr_or_path, *args, **kwargs):
            img = detection._decode(bgr_or_path)
            # Only the PROBE (passed directly as an ndarray by verify()) is
            # ambiguous; paths (the document) keep real detection.
            if img is not None and img.size > 0 and not isinstance(bgr_or_path, (str, Path)):
                return self._two_face_boxes()
            return real_detect(bgr_or_path, *args, **kwargs)

        monkeypatch.setattr(detection, "detect_faces", fake_detect)

        doc = tmp_path / "doc.png"
        probe = tmp_path / "probe.png"
        cv2.imwrite(str(doc), np.full((400, 400, 3), 200, dtype=np.uint8))
        cv2.imwrite(str(probe), np.full((400, 400, 3), 200, dtype=np.uint8))

        result = face_engine.verify(str(doc), str(probe))
        monkeypatch.undo()
        assert result["match_status"] == "INCONCLUSIVE"
        codes = {r.get("code") for r in result["reasons"]}
        assert "probe_multiple_faces" in codes
        # The comparison is refused outright: no similarity is reported.
        assert result["similarity_score"] is None


# ---------------------------------------------------------------------------
# PDF handling
# ---------------------------------------------------------------------------
class TestPdfHandling:
    def test_pdf_upload_is_accepted_and_rasterized(self, auth_client) -> None:
        pdf = _make_pdf_bytes(pages=1)
        up = auth_client.post(
            "/api/screening/upload",
            files={"file": ("scan.pdf", io.BytesIO(pdf), "application/pdf")},
            data={"doc_type_hint": "passport"},
        )
        assert up.status_code == 201, up.text
        body = up.json()
        assert body["file"]["detected_type"] == "pdf"
        assert body["file"].get("pdf_pages") == 1
        # The pipeline must be able to analyze the raster without a 500.
        res = auth_client.post("/api/screening/analyze", json={"run_id": body["run_id"]})
        assert res.status_code == 200
        result = res.json()["result"]
        assert result["final_status"] in {
            "completed", "completed_with_errors", "completed_with_skips",
            "completed_with_degraded_modules",
        }
        assert result["document"]["pdf_pages"] == 1
        assert result["document"]["pdf_pages_screened"] == 1

    def test_multipage_pdf_discloses_partial_coverage(self, auth_client) -> None:
        pdf = _make_pdf_bytes(pages=3)
        up = auth_client.post(
            "/api/screening/upload",
            files={"file": ("big.pdf", io.BytesIO(pdf), "application/pdf")},
            data={"doc_type_hint": "passport"},
        )
        assert up.status_code == 201
        res = auth_client.post("/api/screening/analyze", json={"run_id": up.json()["run_id"]})
        assert res.status_code == 200
        result = res.json()["result"]
        assert result["document"]["pdf_pages"] == 3
        assert result["document"]["pdf_pages_screened"] == 1
        # Risk must acknowledge the incomplete coverage (any reason text).
        reasons = " ".join(result["risk_assessment"].get("reasons") or [])
        assert "page 1 was screened" in reasons or "coverage" in reasons.lower()
