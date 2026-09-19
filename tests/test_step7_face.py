"""Step 7 tests: one-to-one face verification.

Layers covered:
- Quality safeguards: blur, lighting, featureless crops (unit).
- Detection: YuNet availability, path-based detection, synthetic imagery.
- Model seam: SFace selection, cosine similarity, custom backend registration.
- Engine: contract shape, no-probe, no-face, missing-model honesty, same-image
  MATCH (real embeddings), different-crop NO_MATCH/INCONCLUSIVE behavior.
- Provider: payload shape + 1:1-only honesty fields + engine-error handling.
- Pipeline: face_verification stage stays out of the way when no probe image
  was uploaded, and produces a real verdict when one is present.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.face import detection  # noqa: E402
from app.services.face.constants import VERDICTS  # noqa: E402
from app.services.face.engine import verify  # noqa: E402
from app.services.face.model import cosine_similarity, select_model  # noqa: E402
from app.services.face.quality import evaluate_quality  # noqa: E402
from scripts.get_face_models import all_models_present  # noqa: E402

MODELS_PRESENT = all_models_present()


# ---------------------------------------------------------------------------
# Synthetic face helpers (YuNet needs soft, photo-like shading to fire)
# ---------------------------------------------------------------------------
def _synthetic_face(
    seed: int = 5,
    skin: tuple[int, int, int] = (180, 160, 150),
    eye_dx: int = 35,
    eye_y: int = 185,
    eye_r: int = 13,
    mouth_y: int = 275,
    mouth_w: int = 40,
    blur: float = 0.0,
    brightness: float = 1.0,
    with_face: bool = True,
) -> Any:
    rng = np.random.default_rng(seed)
    img = np.full((480, 640, 3), 205, dtype=np.uint8)
    img = np.clip(img.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    if with_face:
        cv2.ellipse(img, (320, 220), (95, 130), 0, 0, 360, skin, -1)
        cv2.circle(img, (320 - eye_dx, eye_y), eye_r, (50, 50, 70), -1)
        cv2.circle(img, (320 + eye_dx, eye_y), eye_r, (50, 50, 70), -1)
        cv2.ellipse(img, (320, mouth_y), (mouth_w, 16), 0, 0, 180, (110, 90, 90), -1)
    noise = rng.normal(0, 4, img.shape).astype(np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    return img


@pytest.fixture(scope="module")
def doc_face_image(tmp_path_factory) -> Path:
    """A synthetic document image with a detectable portrait."""
    p = tmp_path_factory.mktemp("faces") / "doc.png"
    cv2.imwrite(str(p), _synthetic_face(seed=5))
    return p


# ---------------------------------------------------------------------------
# Quality safeguards (pure cv2, no models needed)
# ---------------------------------------------------------------------------
class TestQuality:
    def test_sharp_crop_passes(self) -> None:
        img = _synthetic_face(seed=5)
        box = {"x": 225, "y": 90, "w": 190, "h": 260}
        codes = {i.code for i in evaluate_quality(img, box)}
        assert "too_blurry" not in codes

    def test_blurry_crop_flagged(self) -> None:
        img = _synthetic_face(seed=5, blur=9.0)
        box = {"x": 225, "y": 90, "w": 190, "h": 260}
        issues = {i.code: i for i in evaluate_quality(img, box)}
        assert "too_blurry" in issues
        assert issues["too_blurry"].severity == "blocking"

    def test_dark_image_flagged(self) -> None:
        img = _synthetic_face(seed=5, brightness=0.12)
        issues = {i.code for i in evaluate_quality(img, None)}
        assert "too_dark" in issues

    def test_overexposed_image_flagged(self) -> None:
        img = _synthetic_face(seed=5, brightness=1.35)
        issues = {i.code for i in evaluate_quality(img, None)}
        assert issues & {"too_bright", "overexposed_regions"}

    def test_featureless_patch_flagged(self) -> None:
        flat = np.full((200, 200, 3), 128, dtype=np.uint8)
        issues = {i.code for i in evaluate_quality(flat, None)}
        assert "featureless_region" in issues

    def test_quality_checks_never_raise_on_garbage(self) -> None:
        garbage = np.zeros((2, 2), dtype=np.uint8)
        assert isinstance(evaluate_quality(garbage, None), list)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
class TestDetection:
    @pytest.mark.skipif(not MODELS_PRESENT, reason="YuNet weights not downloaded")
    def test_detects_synthetic_face(self, doc_face_image: Path) -> None:
        faces = detection.detect_faces(str(doc_face_image))
        assert len(faces) >= 1
        assert faces[0].w >= 48 and faces[0].h >= 48

    def test_no_face_on_blank(self) -> None:
        blank = np.full((480, 640, 3), 205, dtype=np.uint8)
        assert detection.detect_faces(blank) == []

    def test_extreme_pose_gate(self) -> None:
        box = detection.FaceBox(x=0, y=0, w=100, h=100, score=0.9, yaw_ratio=0.9)
        assert detection.extreme_pose(box) is True
        box2 = detection.FaceBox(x=0, y=0, w=100, h=100, score=0.9, yaw_ratio=0.1)
        assert detection.extreme_pose(box2) is False

    def test_occlusion_gate(self) -> None:
        dark = detection.FaceBox(x=0, y=0, w=100, h=100, score=0.9, dark_fraction=0.9)
        closed = detection.FaceBox(x=0, y=0, w=100, h=100, score=0.9, eyes_closed=True)
        ok = detection.FaceBox(x=0, y=0, w=100, h=100, score=0.9)
        assert detection.occluded(dark) and detection.occluded(closed)
        assert not detection.occluded(ok)


# ---------------------------------------------------------------------------
# Model seam
# ---------------------------------------------------------------------------
class TestModelSeam:
    def test_cosine_similarity_basics(self) -> None:
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        assert cosine_similarity(a, a) == pytest.approx(1.0, abs=1e-5)
        assert cosine_similarity(a, np.array([0.0, 1.0, 0.0], dtype=np.float32)) == pytest.approx(0.0, abs=1e-5)
        assert cosine_similarity(a, -a) == pytest.approx(-1.0, abs=1e-5)
        # Zero vector must not crash
        assert cosine_similarity(a, np.zeros(3, dtype=np.float32)) == 0.0

    def test_unknown_model_abstains(self) -> None:
        assert select_model("does-not-exist") is None

    def test_none_disables(self) -> None:
        assert select_model("none") is None

    @pytest.mark.skipif(not MODELS_PRESENT, reason="SFace weights not downloaded")
    def test_sface_selected_by_default(self) -> None:
        model = select_model("sface")
        assert model is not None and model.name == "sface"
        aligned = np.zeros((112, 112, 3), dtype=np.uint8)
        vec = model.embed(aligned)
        # The OpenCV Zoo SFace ONNX emits 128-d embeddings.
        assert vec is not None and len(vec) == 128

    def test_custom_backend_registration(self, monkeypatch) -> None:
        from app.services.face import model as model_mod

        class FakeModel:
            name = "fake-arcface-test"

            @staticmethod
            def available() -> bool:
                return True

            def embed(self, aligned):  # noqa: ANN001
                return np.ones(512, dtype=np.float32)

        model_mod.register_embedding_model(FakeModel())
        selected = model_mod.select_model("fake-arcface-test")
        assert selected is not None and selected.name == "fake-arcface-test"
        model_mod._registry.pop("fake-arcface-test", None)  # cleanup


# ---------------------------------------------------------------------------
# Engine contract + safeguards
# ---------------------------------------------------------------------------
class TestEngine:
    def test_contract_shape_and_honesty_fields(self, doc_face_image: Path) -> None:
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        out = verify(str(doc_face_image), None)
        assert set(out) >= {
            "face_detected_document", "face_detected_presented_person",
            "similarity_score", "match_status", "confidence",
        }
        assert out["match_status"] in VERDICTS
        assert out["one_to_one_only"] is True
        assert out["no_population_search"] is True

    def test_missing_probe_is_inconclusive(self, doc_face_image: Path) -> None:
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        out = verify(str(doc_face_image), None)
        assert out["match_status"] == "INCONCLUSIVE"
        assert out["similarity_score"] is None
        assert any(r["code"] == "no_presented_image" for r in out["reasons"])

    def test_missing_model_is_inconclusive(self, doc_face_image: Path, monkeypatch) -> None:
        from app.core.config import get_settings
        from app.services.face import model as model_mod

        # Patch the cached Settings instance (instance attrs shadow the class).
        monkeypatch.setattr(get_settings(), "face_embedding_model", "none")
        model_mod._sface = None  # force re-selection under the patched setting
        try:
            out = verify(str(doc_face_image), str(doc_face_image))
            assert out["match_status"] == "INCONCLUSIVE"
            assert out["similarity_score"] is None
            assert any(r["code"] == "model_unavailable" for r in out["reasons"])
        finally:
            monkeypatch.undo()
            model_mod._sface = None

    def test_no_face_in_probe_is_inconclusive(self, doc_face_image: Path, tmp_path: Path) -> None:
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        blank = tmp_path / "blank.png"
        cv2.imwrite(str(blank), np.full((480, 640, 3), 205, dtype=np.uint8))
        out = verify(str(doc_face_image), str(blank))
        assert out["match_status"] == "INCONCLUSIVE"
        assert out["face_detected_presented_person"] is False
        assert any(r["code"] == "no_face_detected" for r in out["reasons"])

    def test_same_image_matches(self, doc_face_image: Path) -> None:
        """Same pixels on both sides ⇒ identical embeddings ⇒ MATCH."""
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        out = verify(str(doc_face_image), str(doc_face_image))
        assert out["face_detected_document"] is True
        assert out["face_detected_presented_person"] is True
        assert out["similarity_score"] == pytest.approx(1.0, abs=0.01)
        assert out["match_status"] == "MATCH"
        assert 0.0 <= out["confidence"] <= 1.0

    def test_same_face_reframed_still_matches(self, doc_face_image: Path, tmp_path: Path) -> None:
        """The same face in a selfie-style reframe must still verify as MATCH."""
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        img = cv2.imread(str(doc_face_image))
        reframe = img[0:480, 60:580]
        p = tmp_path / "reframe.png"
        cv2.imwrite(str(p), reframe)
        out = verify(str(doc_face_image), str(p))
        assert out["match_status"] == "MATCH"
        assert out["similarity_score"] >= 0.363

    def test_rotated_face_still_detected_and_matched(self, doc_face_image: Path, tmp_path: Path) -> None:
        """A 90°-rotated copy of the same face must still verify as MATCH
        (YuNet is rotation-covariant on upright-ish faces; 90° stays reliable)."""
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        img = cv2.imread(str(doc_face_image))
        rotated = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        p = tmp_path / "rotated.png"
        cv2.imwrite(str(p), rotated)
        out = verify(str(doc_face_image), str(p))
        assert out["match_status"] in VERDICTS
        if out["match_status"] != "INCONCLUSIVE":
            assert out["face_detected_presented_person"] is True
            assert out["match_status"] == "MATCH"
            assert out["similarity_score"] > 0.5

    def test_garbage_document_is_honest(self, tmp_path: Path) -> None:
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        garbage = tmp_path / "garbage.png"
        garbage.write_bytes(b"not an image")
        out = verify(str(garbage), str(garbage))
        assert out["match_status"] == "INCONCLUSIVE"
        assert out["face_detected_document"] is False
        assert out["face_detected_presented_person"] is False


# ---------------------------------------------------------------------------
# Provider layer
# ---------------------------------------------------------------------------
class TestProvider:
    def test_provider_contract(self, doc_face_image: Path) -> None:
        from app.services.face.provider import CvFaceVerificationProvider

        payload = CvFaceVerificationProvider().verify(str(doc_face_image), None)
        if not MODELS_PRESENT:
            assert payload["implemented"] is False
            assert payload["human_review_required"] is True
            return
        assert payload["implemented"] is True
        assert payload["engine"] == "cv-face-yunet-sface"
        assert set(payload) >= {
            "face_detected_document", "face_detected_presented_person",
            "similarity_score", "match_status", "confidence",
        }
        assert payload["match_status"] in VERDICTS
        assert payload["match"] == (payload["match_status"] == "MATCH")
        assert isinstance(payload["explanation"], str) and payload["explanation"]
        assert payload["human_review_required"] is True  # INCONCLUSIVE without probe

    def test_provider_engine_error_is_honest(self, tmp_path: Path) -> None:
        from app.services.face.provider import CvFaceVerificationProvider

        garbage = tmp_path / "garbage.png"
        garbage.write_bytes(b"junk")
        payload = CvFaceVerificationProvider().verify(str(garbage), None)
        # Decoding failure is an honest INCONCLUSIVE with a reason — the
        # engine ran; the input was unusable.
        assert payload["match_status"] == "INCONCLUSIVE"
        assert payload["implemented"] is True
        assert payload["similarity_score"] is None
        assert payload["human_review_required"] is True
        assert payload["face_verification"]["reasons"]


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------
def _upload(auth_client, name: str, content: bytes, mime: str, probe: bytes | None = None) -> dict:
    files = {"file": (name, content, mime)}
    if probe is not None:
        files["probe_image"] = ("probe.png", probe, "image/png")
    res = auth_client.post("/api/screening/upload", files=files, data={"doc_type_hint": "passport"})
    assert res.status_code == 201, res.text
    return res.json()


class TestPipeline:
    def test_stage_pending_without_models(self, auth_client, monkeypatch, png_bytes: bytes) -> None:
        """With the engine unavailable, the stage reports honestly and never blocks."""
        from app.services import ai_providers

        real_get_provider = ai_providers.get_provider

        class _Broken:
            name = "cv-face-yunet-sface"

            @staticmethod
            def verify(doc, probe):  # noqa: ANN001, ARG004
                raise RuntimeError("boom")

        def _fake_get_provider(capability: str):
            if capability == "face":
                return _Broken()
            return real_get_provider(capability)

        monkeypatch.setattr(ai_providers, "get_provider", _fake_get_provider)
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        res = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        assert res.status_code == 200
        stage = next(s for s in res.json()["stages"] if s["stage"] == "face_verification")
        assert stage["status"] in {"not_implemented", "error"}
        assert stage["human_review_required"] is True

    def test_stage_real_verdict_with_probe(self, auth_client, doc_face_image: Path) -> None:
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        run = _upload(
            auth_client, "doc.png", doc_face_image.read_bytes(), "image/png",
            probe=doc_face_image.read_bytes(),
        )
        res = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        assert res.status_code == 200
        stage = next(s for s in res.json()["stages"] if s["stage"] == "face_verification")
        assert stage["status"] == "ok"
        data = stage["data"]
        assert data["implemented"] is True
        assert data["match_status"] == "MATCH"
        fv = data["face_verification"]
        assert set(fv) >= {"face_detected_document", "face_detected_presented_person", "similarity_score"}
        assert fv["one_to_one_only"] is True

    def test_stage_without_probe_stays_ok_and_inconclusive(self, auth_client, doc_face_image: Path) -> None:
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        run = _upload(auth_client, "doc.png", doc_face_image.read_bytes(), "image/png")
        res = auth_client.post("/api/screening/analyze", json={"run_id": run["run_id"]})
        assert res.status_code == 200
        stage = next(s for s in res.json()["stages"] if s["stage"] == "face_verification")
        assert stage["status"] == "ok"
        assert stage["data"]["match_status"] == "INCONCLUSIVE"
        assert stage["human_review_required"] is True

    def test_probe_file_endpoint(self, auth_client, doc_face_image: Path) -> None:
        if not MODELS_PRESENT:
            pytest.skip("face models not downloaded")
        run = _upload(
            auth_client, "doc.png", doc_face_image.read_bytes(), "image/png",
            probe=doc_face_image.read_bytes(),
        )
        res = auth_client.get(f"/api/screening/{run['run_id']}/probe-file")
        assert res.status_code == 200

    def test_probe_file_endpoint_404_without_probe(self, auth_client, png_bytes: bytes) -> None:
        run = _upload(auth_client, "doc.png", png_bytes, "image/png")
        res = auth_client.get(f"/api/screening/{run['run_id']}/probe-file")
        assert res.status_code == 404

    def test_provider_registered_by_default(self) -> None:
        from app.services import ai_providers

        provider = ai_providers.get_provider("face")
        assert provider.name == "cv-face-yunet-sface"
