"""Step 14 tests: SIH demonstration mode.

Layers covered:
- Fixture generation: manifest/case catalog shape, images exist and are
  readable, honesty banners present in the fixtures' README/manifest.
- Case catalog API: GET /api/demo/cases lists the four scripted cases.
- Scripted execution API: POST /api/demo/run/{case_id} for all four cases,
  asserting the SPEC's expected behavior end-to-end through the REAL
  pipeline (OCR → validation → tampering → face → risk → audit).
- Honesty guards: every demo response is DEMONSTRATION/SIMULATED-labeled;
  the stored report stays canonical (no demo markers inside the audited
  payload); demo run_ids are clearly identifiable; no government
  verification is simulated (registry stays MOCK-stamped).
- Face-fixture integrity: the scripted pairs must keep their calibrated
  verdicts (A↔A MATCH, A↔B NO_MATCH) so the demo never lies on stage.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import demo_mode  # noqa: E402
from scripts.make_demo_fixtures import build_manifest, generate  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: generate the demo fixture set once per test session (into the
# repo's data/demo_fixtures; the generator is deterministic).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def demo_fixtures():
    generate()  # idempotent; also (re)writes manifest.json + README.md
    return demo_mode.fixtures_dir()


@pytest.fixture(scope="session")
def cases_payload(client, admin_headers, demo_fixtures):
    res = client.get("/api/demo/cases", headers=admin_headers)
    assert res.status_code == 200, res.text
    return res.json()


def _run_case(auth_client, case_id: str) -> dict:
    res = auth_client.post(f"/api/demo/run/{case_id}")
    assert res.status_code == 200, res.text
    return res.json()


def _result(payload: dict) -> dict:
    return payload["result"]


def _checks(payload: dict) -> list[dict]:
    return _result(payload)["demo"]["checks"]


def _hard_checks(payload: dict) -> list[dict]:
    return [c for c in _checks(payload) if not c.get("informational")]


# ---------------------------------------------------------------------------
# Fixture generation + catalog
# ---------------------------------------------------------------------------
class TestFixtures:
    def test_all_case_files_exist_and_decode(self, demo_fixtures):
        manifest = build_manifest()
        names = ["case1_valid_passport.png", "case2_tampered_passport.jpg",
                 "case3_document_passport.png", "case4_blurred_passport.jpg",
                 "case1_probe_person_a_1.png", "case1_probe_person_a_2.png",
                 "case3_probe_person_b.png"]
        for name in names:
            p = demo_fixtures / name
            assert p.is_file(), f"missing fixture {name}"
            img = cv2.imread(str(p))
            assert img is not None, f"fixture {name} does not decode"

    def test_manifest_covers_four_scripted_cases(self):
        manifest = build_manifest()
        assert set(manifest["cases"]) == {
            "case1_valid", "case2_tampered",
            "case3_identity_mismatch", "case4_low_quality",
        }

    def test_manifest_notice_is_demonstration_only(self):
        notice = build_manifest()["notice"].upper()
        assert "DEMONSTRATION" in notice and "SIMULATED" in notice

    def test_fixture_readme_carries_specimen_banner(self, demo_fixtures):
        text = (demo_fixtures / "README.md").read_text(encoding="utf-8")
        assert "FAKE" in text.upper() or "SPECIMEN" in text.upper()


class TestCatalogApi:
    def test_cases_endpoint_lists_all_four(self, cases_payload):
        assert set(cases_payload["cases"]) == {
            "case1_valid", "case2_tampered",
            "case3_identity_mismatch", "case4_low_quality",
        }
        assert "DEMONSTRATION" in cases_payload["notice"].upper()

    def test_each_case_declares_expected_outcomes(self, cases_payload):
        for case_id, case in cases_payload["cases"].items():
            assert case["title"], case_id
            assert case["expected"], case_id
            assert case["document"]["filename"], case_id
            assert case["probe"]["filename"], case_id


# ---------------------------------------------------------------------------
# Scripted execution: the four cases, end-to-end (real pipeline)
# ---------------------------------------------------------------------------
class TestCase1Valid:
    def test_expected_outcomes(self, auth_client, demo_fixtures):
        payload = _run_case(auth_client, "case1_valid")
        r = _result(payload)
        ocr = r["ocr"] or {}
        assert ocr.get("fields"), "OCR must extract fields"
        assert (r["validation"] or {}).get("failures") == []
        tampering = r["tampering"] or {}
        assert tampering.get("verdict") == "no_obvious_manipulation"
        face = r["face_verification"] or {}
        assert face.get("match_status") == "MATCH"
        assert r["risk_assessment"]["risk_level"] == "LOW"
        assert r["audit"]["event_id"] is not None
        # every hard checkpoint honestly met
        failed = [c["criterion"] for c in _hard_checks(payload) if not c["met"]]
        assert failed == [], f"unmet checkpoints: {failed}"


class TestCase2Tampered:
    def test_expected_outcomes(self, auth_client, demo_fixtures):
        payload = _run_case(auth_client, "case2_tampered")
        r = _result(payload)
        assert (r["ocr"] or {}).get("fields"), "OCR must still extract fields"
        tampering = r["tampering"] or {}
        assert tampering.get("verdict") in {"suspicious", "likely_manipulated"}
        assert tampering.get("indicators"), "tampering indicators expected"
        assert r["risk_assessment"]["risk_score"] > 24
        assert r["human_review_required"] is True
        assert r["audit"]["event_id"] is not None
        failed = [c["criterion"] for c in _hard_checks(payload) if not c["met"]]
        assert failed == [], f"unmet checkpoints: {failed}"


class TestCase3IdentityMismatch:
    def test_expected_outcomes(self, auth_client, demo_fixtures):
        payload = _run_case(auth_client, "case3_identity_mismatch")
        r = _result(payload)
        assert (r["ocr"] or {}).get("fields")
        assert (r["validation"] or {}).get("failures") == []
        face = r["face_verification"] or {}
        assert face.get("match_status") == "NO_MATCH"
        assert face.get("similarity_score") <= 0.25
        assert r["risk_assessment"]["risk_level"] in {"HIGH", "CRITICAL"}
        assert r["human_review_required"] is True
        assert r["audit"]["event_id"] is not None
        failed = [c["criterion"] for c in _hard_checks(payload) if not c["met"]]
        assert failed == [], f"unmet checkpoints: {failed}"


class TestCase4LowQuality:
    def test_expected_outcomes(self, auth_client, demo_fixtures):
        payload = _run_case(auth_client, "case4_low_quality")
        r = _result(payload)
        conf = (r["ocr"] or {}).get("overall_confidence")
        low_conf = conf is None or (isinstance(conf, (int, float)) and conf < 0.6)
        assert low_conf, f"expected low OCR confidence, got {conf}"
        # Honest degradation: human review must be required.
        assert r["human_review_required"] is True
        failed = [c["criterion"] for c in _hard_checks(payload) if not c["met"]]
        assert failed == [], f"unmet checkpoints: {failed}"


# ---------------------------------------------------------------------------
# Honesty guards
# ---------------------------------------------------------------------------
class TestHonesty:
    def test_every_demo_response_is_labeled(self, auth_client, demo_fixtures):
        payload = _run_case(auth_client, "case1_valid")
        assert payload.get("demo_notice", "").upper().count("DEMONSTRATION") >= 1
        r = _result(payload)
        demo = r["demo"]
        assert demo["is_demo"] is True
        assert "DEMONSTRATION" in demo["notice"].upper()
        assert "SIMULATED" in demo["notice"].upper()
        assert "SIMULATED" in r["disclaimer"].upper()

    def test_stored_report_keeps_canonical_shape(self, auth_client, demo_fixtures):
        """The audited payload must not contain demo markers (hash integrity)."""
        from app.db.base import get_session_factory
        from app.services import screening_service

        payload = _run_case(auth_client, "case1_valid")
        run_id = payload["run_id"]
        session = get_session_factory()()
        try:
            row = screening_service.require_screening(session, run_id)
            stored = screening_service.load_report(row)
        finally:
            session.close()
        assert "demo" not in stored
        assert "DEMONSTRATION" not in str(stored.get("disclaimer", "")).upper()

    def test_demo_run_ids_are_identifiable(self, auth_client, demo_fixtures):
        payload = _run_case(auth_client, "case2_tampered")
        assert payload["run_id"].startswith("demo_case2_tampered_")

    def test_no_government_verification_is_simulated(self, auth_client, demo_fixtures):
        """Registry lookups stay MOCK-stamped in demo mode, exactly as in prod."""
        payload = _run_case(auth_client, "case1_valid")
        stages = {s["stage"]: s for s in _result(payload)["stages"]}
        validation = stages.get("document_validation", {})
        raw = str(validation.get("data", {})).upper()
        assert "MOCK" in raw, "registry evidence must stay MOCK-stamped"

    def test_unknown_case_404(self, auth_client, demo_fixtures):
        res = auth_client.post("/api/demo/run/case99_missing")
        assert res.status_code == 404

    def test_demo_requires_auth(self, client, demo_fixtures):
        res = client.get("/api/demo/cases")
        assert res.status_code in {401, 403}


# ---------------------------------------------------------------------------
# Face-fixture integrity (the demo must never lie on the face stage)
# ---------------------------------------------------------------------------
class TestFaceFixtureIntegrity:
    def test_aa_still_matches_and_ab_still_fails(self, demo_fixtures):
        pytest.importorskip("cv2")
        engine = pytest.importorskip("app.services.face.engine")
        from scripts.get_face_models import all_models_present

        if not all_models_present():
            pytest.skip("YuNet/SFace weights not downloaded")
        d = demo_fixtures
        aa = engine.verify(str(d / "case1_valid_passport.png"),
                           str(d / "case1_probe_person_a_1.png"))
        ab = engine.verify(str(d / "case1_valid_passport.png"),
                           str(d / "case3_probe_person_b.png"))
        assert aa["match_status"] == "MATCH"
        assert ab["match_status"] == "NO_MATCH"
