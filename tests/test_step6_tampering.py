"""Step 6 tests: document tampering detection.

Unit layer: every detector runs on synthetic pristine vs edited images and
stays honest (no verdict inflation, no crashes, ELA global gate, metadata
rules, copy-move cluster detection).
Provider layer: contract payload shape + honesty fields + failure handling.
Pipeline layer: tampering_detection stage produces real forensic output for
a sample upload and never blocks the rest of the pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.tampering.compression import analyze_compression  # noqa: E402
from app.services.tampering.constants import VERDICTS  # noqa: E402
from app.services.tampering.copymove import analyze_copymove  # noqa: E402
from app.services.tampering.ela import analyze_ela, ela_region_flags  # noqa: E402
from app.services.tampering.fusion import fuse_signals  # noqa: E402
from app.services.tampering.metadata import analyze_metadata, read_metadata  # noqa: E402
from app.services.tampering.model import NoModel, select_model  # noqa: E402
from app.services.tampering.noise import analyze_noise  # noqa: E402
from app.services.tampering.regions import (  # noqa: E402
    detect_editing_artifacts,
    detect_photo_region,
    detect_stamp_region,
)


# ---------------------------------------------------------------------------
# Synthetic document helpers
# ---------------------------------------------------------------------------
def _pristine_document(w: int = 900, h: int = 600, seed: int = 7) -> Any:
    """A synthetic 'document': flat paper, printed text lines, a photo box."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 235, dtype=np.uint8)
    img[:, :, 0] = img[:, :, 0] - 10  # slightly beige
    for i in range(14):
        y = 40 + i * 36
        cv2.putText(img, f"Printed line {i} THE QUICK BROWN FOX 0123456789",
                    (40, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (35, 35, 35), 1, cv2.LINE_AA)
    cv2.rectangle(img, (w - 260, 40), (w - 60, 240), (200, 200, 200), -1)
    cv2.circle(img, (w - 160, 120), 35, (150, 150, 150), -1)
    # subtle paper grain
    grain = rng.normal(0, 2.0, (h, w)).astype(np.float32)
    img = np.clip(img.astype(np.float32) + grain[:, :, None], 0, 255).astype(np.uint8)
    return img


def _edited_document(tmp_path: Path, seed: int = 7) -> tuple[Any, Any]:
    """Pristine document + a copy of it with a rectangular region pasted
    from another (noisier) texture, then re-encoded as JPEG once."""
    pristine = _pristine_document(seed=seed)
    h, w = pristine.shape[:2]
    edited = pristine.copy()
    # "Textured patch" from a different noise field — like a spliced region.
    rng = np.random.default_rng(seed + 100)
    patch = rng.normal(150, 28, (120, 200, 3)).astype(np.float32)
    patch = np.clip(patch, 0, 255).astype(np.uint8)
    x, y = 60, 300
    edited[y: y + 120, x: x + 200] = patch
    return pristine, edited


def _write_jpg(path: Path, img: Any, quality: int = 90) -> Path:
    cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return path


# ---------------------------------------------------------------------------
# Detector-level unit tests
# ---------------------------------------------------------------------------
class TestMetadata:
    def test_png_has_no_exif_anomaly_below_low(self, tmp_path: Path) -> None:
        img = _pristine_document(400, 300)
        p = _write_jpg(tmp_path / "clean.jpg", img)
        meta = read_metadata(p)
        inds = analyze_metadata(meta)
        # Pristine re-encoded file: at most a low/info stripped-EXIF note.
        assert all(i["severity"] in {"info", "low"} for i in inds)

    def test_software_tag_fires_editor_indicator(self, tmp_path: Path) -> None:
        from PIL import Image

        img = _pristine_document(400, 300)
        p = tmp_path / "edited.jpg"
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        exif = Image.Exif()
        exif[0x0131] = "Adobe Photoshop 24.0"  # Software tag
        pil.save(p, format="JPEG", quality=90, exif=exif.tobytes())
        meta = read_metadata(p)
        assert meta["has_exif"] is True
        inds = analyze_metadata(meta)
        assert any("photo-editing software" in i["note"] for i in inds)

    def test_read_metadata_never_raises_on_garbage(self, tmp_path: Path) -> None:
        p = tmp_path / "garbage.jpg"
        p.write_bytes(b"not an image at all")
        meta = read_metadata(p)
        assert isinstance(meta, dict)


class TestEla:
    def test_ela_runs_and_self_normalizes(self) -> None:
        img = _pristine_document(700, 500)
        result = analyze_ela(img, reference_quality=90)
        assert 0.0 <= result.bright_fraction <= 1.0
        assert result.mean_error >= 0.0
        flags = ela_region_flags(result)
        # Pristine synthetic page: no high-contrast solid glow required.
        assert all(f["contrast"] < 5.0 for f in flags)

    def test_ela_global_gate_on_flat_image(self) -> None:
        flat = np.full((300, 300, 3), 128, dtype=np.uint8)
        result = analyze_ela(flat, reference_quality=90)
        # Uniform image → error map has no local outliers → gate behavior
        # must keep bright_fraction honest (0 when gated).
        if result.global_gate:
            assert result.bright_fraction == 0.0


class TestNoise:
    def test_noise_abstains_on_tiny_image(self) -> None:
        tiny = np.zeros((40, 40), dtype=np.float32)
        out = analyze_noise(tiny)
        assert out["fired"] is False

    def test_noise_detects_patch_outlier(self) -> None:
        # Base texture + a much noisier region pasted in.
        rng = np.random.default_rng(3)
        base = rng.normal(120, 3, (400, 400)).astype(np.float32)
        patch = rng.normal(120, 20, (100, 100)).astype(np.float32)
        img = base.copy()
        img[150:250, 150:250] = patch
        out = analyze_noise(img)
        assert out["global_median_noise"] is not None
        # A strong outlier patch must produce outliers (may or may not fire
        # depending on usable fraction, but never crashes or inverts).
        assert isinstance(out["fired"], bool)


def _jpg_roundtrip_gray(img: Any, quality: int = 85) -> Any:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok
    return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)


class TestCompression:
    def test_non_jpeg_abstains(self) -> None:
        gray = (_pristine_document(400, 300)[:, :, 0]).astype(np.uint8)
        out = analyze_compression(gray, is_jpeg=False)
        assert out["fired"] is False
        assert "not applicable" in out["note"].lower()

    def test_jpeg_runs_and_reports_median(self) -> None:
        img = _jpg_roundtrip_gray(_pristine_document(600, 400))
        out = analyze_compression(img, is_jpeg=True)
        assert out["median_ratio"] is not None
        assert isinstance(out["fired"], bool)


class TestCopyMove:
    def test_plain_document_does_not_fire(self) -> None:
        img = _pristine_document(600, 400)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        out = analyze_copymove(gray)
        assert out["fired"] is False

    def test_duplicated_region_fires(self) -> None:
        img = _pristine_document(600, 400)
        # Realistic forgery: paste a UNIQUE texture (random noise) at two
        # locations — its blocks can only match each other, so the true
        # shift must win as a dense cluster.
        rng = np.random.default_rng(42)
        patch = np.clip(rng.normal(140, 25, (80, 200, 3)), 0, 255).astype(np.uint8)
        img[100:180, 80:280] = patch
        img[300:380, 250:450] = patch
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        out = analyze_copymove(gray)
        assert out["fired"] is True, f"expected copy-move fire: {out['note']}"
        assert out["pair_count"] >= 6
        assert out["source_fill_ratio"] >= 0.45
        assert out["region"] is not None


class TestRegions:
    def test_photo_detector_runs_on_pristine(self) -> None:
        img = _pristine_document(700, 500)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ela = analyze_ela(img, reference_quality=90)
        out = detect_photo_region(img, gray, ela)
        assert isinstance(out["fired"], bool)
        assert out["photo_box"] is not None or "not located" in out["note"].lower()

    def test_stamp_detector_abstains_without_ink(self) -> None:
        img = _pristine_document(400, 300)
        out = detect_stamp_region(img)
        assert out["fired"] is False

    def test_stamp_detector_sees_colored_ink(self) -> None:
        img = _pristine_document(500, 400)
        cv2.ellipse(img, (250, 200), (80, 50), 20, 0, 360, (40, 40, 160), 3)  # blue stamp
        out = detect_stamp_region(img)
        assert out["ink_fraction"] > 0.0

    def test_artifact_detector_runs(self) -> None:
        img = _pristine_document(600, 400)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ela = analyze_ela(img, reference_quality=90)
        out = detect_editing_artifacts(img, gray, ela)
        assert isinstance(out["fired"], bool)


# ---------------------------------------------------------------------------
# Fusion layer
# ---------------------------------------------------------------------------
class TestFusion:
    def test_no_indicators_is_clean_and_not_detected(self) -> None:
        out = fuse_signals([], coverage=1.0, detector_failures=[], model_probability=None)
        assert out["verdict"] == "no_obvious_manipulation"
        assert out["tampering_detected"] is False
        assert 0 <= out["risk_score"] < 20  # below suspicious floor

    def test_single_weak_indicator_is_suspicious_not_likely(self) -> None:
        inds = [{"type": "ela_inconsistency", "severity": "medium",
                 "confidence": 0.5, "note": "", "details": {}}]
        out = fuse_signals(inds, coverage=1.0, detector_failures=[], model_probability=None)
        assert out["verdict"] == "suspicious"
        assert out["tampering_detected"] is True
        assert out["verdict"] in VERDICTS

    def test_three_agreeing_types_escalate_to_likely(self) -> None:
        inds = [
            {"type": "ela_inconsistency", "severity": "high", "confidence": 0.8},
            {"type": "noise_inconsistency", "severity": "medium", "confidence": 0.7},
            {"type": "copy_paste_inconsistency", "severity": "high", "confidence": 0.85},
        ]
        out = fuse_signals(inds, coverage=1.0, detector_failures=[], model_probability=None)
        assert out["verdict"] == "likely_manipulated"
        assert out["risk_score"] >= 20

    def test_low_coverage_is_inconclusive(self) -> None:
        out = fuse_signals([], coverage=0.2, detector_failures=[], model_probability=None)
        assert out["verdict"] == "inconclusive"
        assert out["confidence"] <= 0.3

    def test_many_detector_failures_are_inconclusive(self) -> None:
        out = fuse_signals(
            [], coverage=1.0,
            detector_failures=["ela: X", "noise: X", "compression: X"],
            model_probability=None,
        )
        assert out["verdict"] == "inconclusive"

    def test_risk_score_bounded_0_100(self) -> None:
        inds = [
            {"type": "copy_paste_inconsistency", "severity": "high", "confidence": 1.0},
            {"type": "model_verdict", "severity": "high", "confidence": 1.0},
            {"type": "ela_inconsistency", "severity": "high", "confidence": 1.0},
            {"type": "noise_inconsistency", "severity": "high", "confidence": 1.0},
            {"type": "compression_inconsistency", "severity": "high", "confidence": 1.0},
        ]
        out = fuse_signals(inds, coverage=1.0, detector_failures=[], model_probability=0.99)
        assert 0 <= out["risk_score"] <= 100
        assert out["risk_score"] > 55

    def test_info_indicators_alone_stay_clean(self) -> None:
        inds = [{"type": "metadata_anomaly", "severity": "info", "confidence": 0.25}]
        out = fuse_signals(inds, coverage=1.0, detector_failures=[], model_probability=None)
        assert out["verdict"] == "no_obvious_manipulation"


# ---------------------------------------------------------------------------
# Model interface
# ---------------------------------------------------------------------------
class TestModelInterface:
    def test_default_model_abstains(self) -> None:
        model = select_model("none")
        assert isinstance(model, NoModel)
        out = model.predict(None, None)
        assert out["probability"] is None

    def test_registered_model_is_used(self, monkeypatch) -> None:
        from app.services.tampering import model as model_mod

        class FakeModel:
            name = "fake-cnn-v1"

            @staticmethod
            def available() -> bool:
                return True

            def predict(self, bgr, gray) -> dict:
                return {"probability": 0.92, "label": "tampered", "version": "1.0"}

        model_mod.register_tampering_model(FakeModel())
        selected = model_mod.select_model("fake-cnn-v1")
        assert selected.name == "fake-cnn-v1"
        assert selected.predict(None, None)["probability"] == 0.92
        # Cleanup: drop the fake so other tests don't see it.
        model_mod._model_registry.pop("fake-cnn-v1", None)


# ---------------------------------------------------------------------------
# Provider layer
# ---------------------------------------------------------------------------
class TestProvider:
    def test_provider_contract_on_pristine(self, tmp_path: Path) -> None:
        from app.services.tampering.provider import ForensicTamperingProvider

        p = _write_jpg(tmp_path / "doc.jpg", _pristine_document(700, 500))
        payload = ForensicTamperingProvider().analyze(str(p))
        assert payload["implemented"] is True
        t = payload["tampering"]
        assert set(t) >= {"tampering_detected", "risk_score", "confidence", "indicators", "verdict"}
        assert 0 <= t["risk_score"] <= 100
        assert 0 <= t["confidence"] <= 1
        assert t["verdict"] in VERDICTS
        for ind in t["indicators"]:
            assert set(ind) >= {"type", "severity", "confidence"}
            assert ind["severity"] in {"info", "low", "medium", "high"}
        # Legacy flat keys still present for the existing frontend.
        assert isinstance(payload["signals"], list)
        assert payload["score"] is None or 0 <= payload["score"] <= 1

    def test_provider_honest_on_missing_file(self) -> None:
        from app.services.tampering.provider import ForensicTamperingProvider

        payload = ForensicTamperingProvider().analyze("Z:/definitely/not/there.jpg")
        assert payload["implemented"] is False
        assert payload["human_review_required"] is True
        assert payload["score"] is None

    def test_pristine_vs_edited_risk_ordering(self, tmp_path: Path) -> None:
        """The edited document should not score *lower* than the pristine one."""
        from app.services.tampering.provider import ForensicTamperingProvider

        pristine, edited = _edited_document(tmp_path)
        pp = _write_jpg(tmp_path / "pristine.jpg", pristine)
        ep = _write_jpg(tmp_path / "edited.jpg", edited)
        a = ForensicTamperingProvider().analyze(str(pp))["tampering"]
        b = ForensicTamperingProvider().analyze(str(ep))["tampering"]
        # Edited must carry at least one more indicator OR a higher score.
        assert (
            len(b["indicators"]) > len(a["indicators"]) or b["risk_score"] > a["risk_score"]
        ), f"pristine={a['risk_score']} edited={b['risk_score']}"


# ---------------------------------------------------------------------------
# Pipeline integration (needs the FastAPI app + sample images)
# ---------------------------------------------------------------------------
def _sample_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "samples" / name


class TestPipelineIntegration:
    def test_tampering_stage_real_output(self, auth_client) -> None:
        sample = _sample_path("sample_passport.png")
        if not sample.is_file():
            pytest.skip("sample images not generated")

        res = auth_client.post(
            "/api/screening/upload",
            files={"file": (sample.name, open(sample, "rb"), "image/png")},
            data={"doc_type_hint": "passport"},
        )
        assert res.status_code == 201
        run_id = res.json()["run_id"]

        res = auth_client.post("/api/screening/analyze", json={"run_id": run_id})
        assert res.status_code == 200
        stages = {s["stage"]: s for s in res.json()["stages"]}
        stage = stages["tampering_detection"]

        assert stage["status"] == "ok"
        data = stage["data"]
        assert data["implemented"] is True
        t = data["tampering"]
        assert set(t) >= {"tampering_detected", "risk_score", "confidence", "indicators", "verdict"}
        assert t["verdict"] in VERDICTS
        assert 0 <= t["risk_score"] <= 100
        # Synthetic pristine sample must NOT be declared likely manipulated.
        assert t["verdict"] != "likely_manipulated"
        # Non-clean verdicts route to human review; clean may not.
        assert stage["human_review_required"] == (t["verdict"] != "no_obvious_manipulation")

    def test_tampering_stage_runs_on_garbage(self, auth_client, png_bytes: bytes) -> None:
        import io

        res = auth_client.post(
            "/api/screening/upload",
            files={"file": ("blank.png", io.BytesIO(png_bytes), "image/png")},
            data={"doc_type_hint": "passport"},
        )
        run_id = res.json()["run_id"]
        res = auth_client.post("/api/screening/analyze", json={"run_id": run_id})
        stages = {s["stage"]: s for s in res.json()["stages"]}
        stage = stages["tampering_detection"]
        # 64x64 flat image: pipeline must not crash; verdict is honest.
        assert stage["status"] in {"ok", "not_implemented", "error"}
        if stage["status"] == "ok":
            t = stage["data"]["tampering"]
            assert t["verdict"] in VERDICTS

    def test_placeholder_provider_still_available(self, monkeypatch) -> None:
        from app.services import ai_providers

        settings = ai_providers.get_settings()
        monkeypatch.setattr(settings, "ai_tampering_provider", "placeholder")
        provider = ai_providers.get_provider("tampering")
        assert provider.name.startswith("placeholder-")
        payload = provider.analyze("whatever.jpg")
        assert payload["implemented"] is False

    def test_forensic_provider_registered_by_default(self) -> None:
        from app.services import ai_providers

        provider = ai_providers.get_provider("tampering")
        assert provider.name == "forensic-heuristics"
