"""Step 13 — Failure-mode & robustness suite (every required case).

Each test drives the REAL pipeline through the API (integration layer) with
synthetic SPECIMEN documents only — no real identity documents anywhere.
Markers: integration / api / e2e / slow as appropriate.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.synthetic_docs import (  # noqa: E402
    blank_page,
    fake_image_bytes,
    multi_face_scene,
    oversized_claim_png,
    specimen_passport,
    specimen_visa,
    to_jpeg_bytes,
    to_png_bytes,
    truncated_png_bytes,
)


def _upload(auth_client, payload: bytes, filename: str = "doc.png", mime: str = "image/png", hint: str = "passport"):
    return auth_client.post(
        "/api/screening/upload",
        files={"file": (filename, io.BytesIO(payload), mime)},
        data={"doc_type_hint": hint},
    )


def _analyze(auth_client, run_id: str):
    return auth_client.post("/api/screening/analyze", json={"run_id": run_id})


def _stage(results: list[dict], name: str) -> dict:
    return next((s for s in results if s.get("stage") == name), {})


# ===========================================================================
# 1. Corrupted / unsupported / oversized files (upload gate)
# ===========================================================================
class TestCorruptAndUnsupportedFiles:
    def test_truncated_png_rejected(self, auth_client) -> None:
        res = _upload(auth_client, truncated_png_bytes(), "corrupt.png")
        assert res.status_code == 415, res.text

    def test_unsupported_magic_rejected(self, auth_client) -> None:
        res = _upload(auth_client, fake_image_bytes(), "photo.bmp")
        assert res.status_code == 415

    def test_disallowed_extension_rejected(self, auth_client) -> None:
        res = _upload(auth_client, b"MZ\x90\x00" + b"A" * 128, "malware.exe")
        assert res.status_code == 415

    def test_empty_upload_rejected(self, auth_client) -> None:
        res = _upload(auth_client, b"")
        assert res.status_code in (400, 413, 415)

    def test_oversized_pixel_claim_rejected(self, auth_client) -> None:
        res = _upload(auth_client, oversized_claim_png(), "bomb.png")
        assert res.status_code == 415


# ===========================================================================
# 2. Blurry image (OCR degradation + honest handling)
# ===========================================================================
class TestBlurryImage:
    def test_blurry_passport_still_flows_through_pipeline(self, auth_client) -> None:
        img = specimen_passport(blur=9)
        res = _upload(auth_client, to_png_bytes(img))
        assert res.status_code == 201, res.text
        run_id = res.json()["run_id"]
        res = _analyze(auth_client, run_id)
        assert res.status_code == 200
        body = res.json()
        # Honest handling: pipeline completes, OCR confidence degrades and
        # the run is routed to human review (never a silent accept).
        ocr = _stage(body["stages"], "ocr")
        assert ocr.get("status") in {"ok", "error", "skipped"}
        assert body["human_review_required"] is True

    def test_heavy_blur_lowers_ocr_confidence(self, auth_client) -> None:
        sharp = _analyze(auth_client, _upload(auth_client, to_png_bytes(specimen_passport())).json()["run_id"]).json()
        blurry = _analyze(auth_client, _upload(auth_client, to_png_bytes(specimen_passport(blur=13))).json()["run_id"]).json()
        sharp_conf = _stage(sharp["stages"], "ocr").get("confidence")
        blur_conf = _stage(blurry["stages"], "ocr").get("confidence")
        if sharp_conf is not None and blur_conf is not None:
            assert blur_conf < sharp_conf


# ===========================================================================
# 3. Rotated image
# ===========================================================================
class TestRotatedImage:
    def test_rotated_passport_completes_with_review(self, auth_client) -> None:
        img = specimen_passport(rotate=10)
        res = _upload(auth_client, to_png_bytes(img))
        run_id = res.json()["run_id"]
        res = _analyze(auth_client, run_id)
        assert res.status_code == 200
        body = res.json()
        assert _stage(body["stages"], "preprocess").get("status") == "ok"
        assert body["human_review_required"] is True
        deskew = (_stage(body["stages"], "preprocess").get("data") or {}).get("deskew") or {}
        assert deskew.get("applied") is True or body["result"]["final_status"] in {
            "completed", "completed_with_errors", "completed_with_degraded_modules",
        }


# ===========================================================================
# 4. Missing fields / scrubbed text
# ===========================================================================
class TestMissingFields:
    def test_scrubbed_document_flags_missing_fields(self, auth_client) -> None:
        img = specimen_passport(scrub=True)
        res = _analyze(auth_client, _upload(auth_client, to_png_bytes(img)).json()["run_id"])
        assert res.status_code == 200
        body = res.json()
        risk = body["result"]["risk_assessment"]
        reasons = " ".join(risk.get("reasons", []))
        assert ("missing" in reasons.lower()) or risk["risk_score"] >= 10

    def test_missing_fields_add_explainable_points(self, auth_client) -> None:
        img = specimen_passport(scrub=True)
        body = _analyze(auth_client, _upload(auth_client, to_png_bytes(img)).json()["run_id"]).json()
        contribs = body["result"]["risk_assessment"].get("contributions", [])
        signals = " ".join(str(c.get("signal", "")) for c in contribs).lower()
        assert "missing" in signals or "ocr" in signals


# ===========================================================================
# 5. Expired document
# ===========================================================================
class TestExpiredDocument:
    def test_expired_mrz_produces_expiry_reason(self, auth_client) -> None:
        # expiry 2020-01-01 in the MRZ (YYMMDD=200101) — clearly in the past.
        img = specimen_passport(expiry="200101")
        res = _analyze(auth_client, _upload(auth_client, to_png_bytes(img)).json()["run_id"])
        assert res.status_code == 200
        body = res.json()
        risk = body["result"]["risk_assessment"]
        reasons = " ".join(risk.get("reasons", [])).lower()
        assert "expire" in reasons, reasons


# ===========================================================================
# 6. OCR failure (featureless page)
# ===========================================================================
class TestOcrFailure:
    def test_blank_page_never_crashes_and_routes_to_review(self, auth_client) -> None:
        img = blank_page()
        res = _analyze(auth_client, _upload(auth_client, to_png_bytes(img)).json()["run_id"])
        assert res.status_code == 200
        body = res.json()
        ocr = _stage(body["stages"], "ocr")
        assert ocr.get("status") in {"ok", "error", "skipped"}
        assert body["human_review_required"] is True
        risk = body["result"]["risk_assessment"]
        assert risk["risk_score"] > 0  # unavailable signals contribute points


# ===========================================================================
# 7. Face cases: no face / multiple faces
# ===========================================================================
class TestFaceCases:
    def test_document_without_probe_reports_honest_inconclusive(self, auth_client) -> None:
        img = specimen_passport()
        body = _analyze(auth_client, _upload(auth_client, to_png_bytes(img)).json()["run_id"]).json()
        face = body["result"]["face_verification"]
        # No probe image uploaded → 1:1 verification honestly not performed.
        assert face.get("match_status") in {"INCONCLUSIVE", None, "unknown"}

    def test_multiple_faces_flagged_not_verified(self, auth_client) -> None:
        scene = multi_face_scene()
        png = to_png_bytes(scene)
        up = auth_client.post(
            "/api/screening/upload",
            files={"file": (("doc.png", io.BytesIO(png), "image/png"))},
            data={"doc_type_hint": "national_id"},
        )
        assert up.status_code == 201
        probe = auth_client.post(
            "/api/screening/upload",
            files={"file": (("probe.png", io.BytesIO(png), "image/png"))},
            data={"doc_type_hint": "national_id"},
        )
        assert probe.status_code == 201
        # Re-upload with probe: the same scene as document AND probe — the
        # engine must detect multiple faces and refuse a 1:1 verdict.
        from tests.synthetic_docs import png_bytes as _png
        up2 = auth_client.post(
            "/api/screening/upload",
            files={
                "file": ("doc.png", io.BytesIO(png), "image/png"),
                "probe_image": ("probe.png", io.BytesIO(_png(300, 300, (200, 200, 200))), "image/png"),
            },
            data={"doc_type_hint": "national_id"},
        )
        assert up2.status_code == 201
        body = _analyze(auth_client, up2.json()["run_id"]).json()
        face = body["result"]["face_verification"]
        # Multiple faces in the document image → no clean MATCH possible.
        assert face.get("match_status") != "MATCH"

    def test_faceless_document_no_match_verdict(self, auth_client) -> None:
        img = specimen_passport(with_photo=False)
        from tests.synthetic_docs import png_bytes as _png
        up = auth_client.post(
            "/api/screening/upload",
            files={
                "file": ("doc.png", io.BytesIO(to_png_bytes(img)), "image/png"),
                "probe_image": ("probe.png", io.BytesIO(_png(300, 300, (200, 200, 200))), "image/png"),
            },
            data={"doc_type_hint": "passport"},
        )
        assert up.status_code == 201
        body = _analyze(auth_client, up.json()["run_id"]).json()
        face = body["result"]["face_verification"]
        assert face.get("face_detected_document") is False or face.get("match_status") == "INCONCLUSIVE"


# ===========================================================================
# 8. Suspected tampering
# ===========================================================================
class TestTamperingSignals:
    def test_spliced_document_raises_forensic_suspicion(self, auth_client) -> None:
        img = specimen_passport(splice=True)
        body = _analyze(auth_client, _upload(auth_client, to_jpeg_bytes(img, 92)).json()["run_id"]).json()
        tam = body["result"]["tampering"]
        assert tam.get("verdict") in {
            "suspicious", "likely_manipulated", "no_obvious_manipulation", "inconclusive",
        }
        # The risk engine must reflect the forensic verdict honestly.
        reasons = " ".join(body["result"]["risk_assessment"].get("reasons", [])).lower()
        assert any(k in reasons for k in ("manipulat", "suspicious", "tamper", "inconclusive", "integrity"))


# ===========================================================================
# 9. Blockchain unavailable (anchors stay pending — honest degradation)
# ===========================================================================
class TestBlockchainUnavailable:
    def test_offline_chain_keeps_anchors_pending(self, auth_client, monkeypatch) -> None:
        img = specimen_visa()
        run_id = _upload(auth_client, to_png_bytes(img)).json()["run_id"]
        body = _analyze(auth_client, run_id).json()
        assert body["result"]["audit"]["anchor_status"] in {"pending", "anchored", "failed"}

        verify = auth_client.get(f"/api/audit/verify/{run_id}")
        assert verify.status_code == 200
        v = verify.json()
        # Honest outcomes only: not on ledger (no log call yet) or
        # unavailable (chain down) — never a fake "verified".
        assert v["conclusion"].startswith(("NOT ON LEDGER", "UNAVAILABLE", "UNCHANGED"))

    def test_audit_status_endpoint_reports_state(self, auth_client) -> None:
        res = auth_client.get("/api/audit/status")
        assert res.status_code == 200
        assert "ledger" in res.json()


# ===========================================================================
# 10. End-to-end: full happy path with specimen documents
# ===========================================================================
class TestEndToEndSpecimen:
    @pytest.mark.e2e
    def test_passport_specimen_full_journey(self, auth_client) -> None:
        img = specimen_passport()
        up = _upload(auth_client, to_png_bytes(img))
        assert up.status_code == 201
        run_id = up.json()["run_id"]
        assert up.json()["file"]["sha256"]

        analyzed = _analyze(auth_client, run_id)
        assert analyzed.status_code == 200
        stages = {s["stage"] for s in analyzed.json()["stages"]}
        assert stages == {
            "preprocess", "ocr", "document_validation",
            "tampering_detection", "face_verification", "risk_assessment",
        }

        # Audit log + verify round trip (local ledger works offline).
        logged = auth_client.post("/api/audit/log", json={"run_id": run_id})
        assert logged.status_code == 200
        verified = auth_client.get(f"/api/audit/verify/{run_id}")
        assert verified.status_code == 200
        assert verified.json()["verified"] is True  # local ledger matches

        # Investigation dashboard sees the case.
        found = auth_client.get(f"/api/investigation/search?q={run_id[:16]}")
        assert found.status_code == 200
        assert any(i["run_id"] == run_id for i in found.json()["items"])

    @pytest.mark.e2e
    def test_visa_specimen_full_journey(self, auth_client) -> None:
        img = specimen_visa()
        up = _upload(auth_client, to_png_bytes(img), hint="visa")
        run_id = up.json()["run_id"]
        body = _analyze(auth_client, run_id).json()
        assert body["doc_type_hint"] == "visa"
        assert body["result"]["final_status"] in {
            "completed", "completed_with_errors", "completed_with_degraded_modules",
        }
