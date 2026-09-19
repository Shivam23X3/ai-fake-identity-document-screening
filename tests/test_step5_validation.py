"""Step 5 tests: document validation rule engine.

Unit layer: rule primitives, ruleset resolution, engine aggregation.
Provider layer: MOCK registry stamping, contract payload shape.
Pipeline layer: document_validation stage produces real checks for
passport/visa uploads and honestly flags missing fields.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.make_samples import passport_mrz, visa_mrz  # noqa: E402

from app.services.validation.checks import STATUS_FAIL, STATUS_PASS, STATUS_WARNING  # noqa: E402
from app.services.validation.engine import ValidationEngine  # noqa: E402
from app.services.validation.rulesets import (  # noqa: E402
    get_ruleset,
    register_ruleset,
)
from app.services.validation.rules import (  # noqa: E402
    date_fields,
    expiry_validity,
    mrz_cross_consistency,
    mrz_doc_code_consistency,
    regex_format,
    required_fields,
    visa_entry_validity,
    visa_stay_duration,
)


def _run(rules, fields, ctx=None):
    from app.services.validation.checks import ValidationOutcome

    outcome = ValidationOutcome(is_valid=True, doc_type="test")
    for rule in rules:
        rule(outcome, fields, ctx or {})
    return outcome


# ---------------------------------------------------------------------------
# Rule primitives
# ---------------------------------------------------------------------------
def test_required_fields_pass_and_fail() -> None:
    fields = {"full_name": {"value": "ALICE SMITH", "confidence": 0.9}}
    outcome = _run([required_fields("full_name", "passport_number")], fields)
    statuses = {c.field: c.status for c in outcome.checks}
    assert statuses["full_name"] == STATUS_PASS
    assert statuses["passport_number"] == STATUS_FAIL
    assert outcome.is_valid is True  # aggregation happens in the engine


def test_date_format_rule() -> None:
    fields = {
        "good": {"value": "1974-08-12", "confidence": 0.9},
        "bad": {"value": "12/08/1974", "confidence": 0.9},
        "absent": {"confidence": 0.0},
    }
    outcome = _run([date_fields("good", "bad", "absent")], fields)
    statuses = {c.field: c.status for c in outcome.checks}
    assert statuses["good"] == STATUS_PASS
    assert statuses["bad"] == STATUS_FAIL
    assert statuses["absent"] == STATUS_WARNING  # absent ≠ invalid


def test_expiry_validity_expired_is_warning() -> None:
    fields = {"expiry_date": {"value": "2020-01-15", "confidence": 0.9}}
    outcome = _run([expiry_validity("expiry_date")], fields)
    assert outcome.checks[0].status == STATUS_WARNING
    assert "expired" in outcome.checks[0].message.lower()


def test_mrz_consistency_mismatch_fails() -> None:
    fields = {"date_of_birth": {"value": "1974-08-12", "confidence": 0.9}}
    ctx = {"mrz_fields": {"date_of_birth": {"value": "1975-08-12", "check_digit_ok": True}}}
    outcome = _run([mrz_cross_consistency([("date_of_birth", "date_of_birth")])], fields, ctx)
    assert outcome.checks[0].status == STATUS_FAIL


def test_mrz_consistency_agreement_passes() -> None:
    fields = {"passport_number": {"value": "L898902C3", "confidence": 0.9}}
    ctx = {"mrz_fields": {"passport_number": {"value": "L898902C3", "check_digit_ok": True}}}
    outcome = _run([mrz_cross_consistency([("passport_number", "passport_number")])], fields, ctx)
    assert outcome.checks[0].status == STATUS_PASS


def test_mrz_doc_code_rule() -> None:
    outcome = _run(
        [mrz_doc_code_consistency()],
        {},
        {"doc_type": "passport", "mrz_fields": {"document_code": {"value": "P"}}},
    )
    assert outcome.checks[0].status == STATUS_PASS
    outcome = _run(
        [mrz_doc_code_consistency()],
        {},
        {"doc_type": "visa", "mrz_fields": {"document_code": {"value": "P"}}},
    )
    assert outcome.checks[0].status == STATUS_FAIL


def test_visa_entry_and_stay_rules() -> None:
    fields = {
        "entry_validity": {"value": "multiple", "confidence": 0.9},
        "stay_duration": {"value": "90 days", "confidence": 0.9},
    }
    outcome = _run([visa_entry_validity(), visa_stay_duration()], fields)
    assert all(c.status == STATUS_PASS for c in outcome.checks)


def test_visa_stay_absurd_duration_warns() -> None:
    fields = {"stay_duration": {"value": "500 days", "confidence": 0.9}}
    outcome = _run([visa_stay_duration()], fields)
    assert outcome.checks[0].status == STATUS_WARNING


def test_format_rule_pass_and_fail() -> None:
    fields = {
        "passport_number": {"value": "L898902C3", "confidence": 0.9},
        "junk": {"value": "!!!", "confidence": 0.9},
    }
    outcome = _run([regex_format("passport_number", r"[A-Z]{1,2}[A-Z0-9]{6,9}", "test shape")], fields)
    by_field = {c.field: c for c in outcome.checks}
    assert by_field["passport_number"].status == STATUS_PASS
    outcome = _run([regex_format("junk", r"[A-Z]{1,2}[A-Z0-9]{6,9}", "test shape")], fields)
    assert outcome.checks[0].status == STATUS_FAIL


# ---------------------------------------------------------------------------
# Ruleset registry (extensibility contract)
# ---------------------------------------------------------------------------
def test_ruleset_resolution_specific_over_generic() -> None:
    passport = get_ruleset("passport")
    assert passport is not None and passport.doc_type == "passport"
    assert any("mrz" in getattr(r, "__name__", "") for r in passport.rules)

    # Unknown doc type falls back to the generic ruleset.
    generic = get_ruleset("diploma")
    assert generic is not None and generic.doc_type == "unknown"


def test_country_ruleset_overrides_generic() -> None:
    from app.services.validation.rulesets import Ruleset

    custom = Ruleset(
        doc_type="passport",
        country="UTO",
        description="Utopia-specific rules (synthetic example).",
        country_real_rules=False,
        rules=[required_fields("passport_number")],
    )
    register_ruleset(custom)
    resolved = get_ruleset("passport", country="UTO")
    assert resolved is custom
    # Other countries still get the generic passport ruleset.
    assert get_ruleset("passport", country="XYZ") is not custom


def test_engine_aggregates_is_valid() -> None:
    fields = {
        "full_name": {"value": "X", "confidence": 0.9},
        "passport_number": {"value": None, "confidence": 0.0},
    }
    engine = ValidationEngine()
    outcome = engine.validate(fields, doc_type="passport")
    assert outcome.is_valid is False
    assert any(c.status == STATUS_FAIL for c in outcome.checks)


def test_engine_confidence_reflects_field_confidence() -> None:
    engine = ValidationEngine()
    good = engine.validate(
        {"passport_number": {"value": "L898902C3", "confidence": 0.9},
         "nationality": {"value": "UTO", "confidence": 0.9}},
        doc_type="passport",
    )
    assert good.confidence is not None and good.confidence >= 0.3


def test_engine_survives_crashing_rule() -> None:
    from app.services.validation.checks import ValidationCheck, ValidationOutcome
    from app.services.validation.rulesets import Ruleset

    def boom(outcome: ValidationOutcome, fields, ctx) -> None:  # noqa: ARG001
        raise RuntimeError("boom")

    register_ruleset(Ruleset(doc_type="boom", rules=[boom]))
    engine = ValidationEngine()
    outcome = engine.validate({}, doc_type="boom")
    assert any(c.rule == "boom" and c.status == STATUS_WARNING for c in outcome.checks)
    assert outcome.is_valid is True  # a crashing rule must not fail the doc


def test_engine_contract_payload_shape() -> None:
    """The Step-5 contract: {is_valid, checks[{field,status,message}], confidence}."""
    engine = ValidationEngine()
    outcome = engine.validate({}, doc_type="passport")
    payload = outcome.to_dict()
    assert set(payload) >= {"is_valid", "checks", "confidence"}
    for check in payload["checks"]:
        assert set(check) >= {"field", "status", "message"}
        assert check["status"] in {"PASS", "FAIL", "WARNING"}


# ---------------------------------------------------------------------------
# Full passport / visa scenarios through the engine
# ---------------------------------------------------------------------------
def _passport_fields() -> dict:
    return {
        "full_name": {"value": "ALICE JANE SMITH", "confidence": 0.9},
        "passport_number": {"value": "L898902C3", "confidence": 0.95, "check_digit_ok": True},
        "nationality": {"value": "UTO", "confidence": 0.9},
        "date_of_birth": {"value": "1974-08-12", "confidence": 0.93, "check_digit_ok": True},
        "expiry_date": {"value": "2028-04-15", "confidence": 0.92, "check_digit_ok": True},
    }


def _passport_mrz_ctx() -> dict:
    l1, l2 = passport_mrz()
    return {
        "mrz_raw": f"{l1}\n{l2}",
        "mrz_fields": {
            "document_code": {"value": "P", "check_digit_ok": None},
            "passport_number": {"value": "L898902C3", "check_digit_ok": True},
            "nationality": {"value": "UTO", "check_digit_ok": None},
            "date_of_birth": {"value": "1974-08-12", "check_digit_ok": True},
            "expiry_date": {"value": "2028-04-15", "check_digit_ok": True},
        },
    }


def test_valid_passport_scenario() -> None:
    engine = ValidationEngine()
    outcome = engine.validate(_passport_fields(), doc_type="passport", **_passport_mrz_ctx())
    assert outcome.is_valid is True
    statuses = {(c.field, c.status) for c in outcome.checks}
    assert (STATUS_FAIL, None) not in [(s, None) for f, s in statuses]
    assert not outcome.failures
    # No FAIL statuses at all
    assert all(c.status != STATUS_FAIL for c in outcome.checks)


def test_passport_with_bad_check_digit_fails() -> None:
    fields = _passport_fields()
    fields["date_of_birth"]["check_digit_ok"] = False
    engine = ValidationEngine()
    outcome = engine.validate(fields, doc_type="passport", **_passport_mrz_ctx())
    assert outcome.is_valid is False
    dob_checks = [c for c in outcome.checks if c.field == "date_of_birth" and c.status == STATUS_FAIL]
    assert dob_checks, "check-digit mismatch must FAIL"


def test_passport_missing_required_field_fails() -> None:
    fields = _passport_fields()
    del fields["nationality"]
    engine = ValidationEngine()
    outcome = engine.validate(fields, doc_type="passport", **_passport_mrz_ctx())
    assert outcome.is_valid is False


def test_valid_visa_scenario() -> None:
    fields = {
        "full_name": {"value": "KARL THEO SEDERBERG", "confidence": 0.9},
        "visa_number": {"value": "AB9876543", "confidence": 0.94, "check_digit_ok": True},
        "visa_type": {"value": "C-1 WORK", "confidence": 0.8},
        "entry_validity": {"value": "multiple", "confidence": 0.9},
        "stay_duration": {"value": "90 days", "confidence": 0.9, "check_digit_ok": True},
        "date_of_birth": {"value": "1985-06-15", "confidence": 0.93, "check_digit_ok": True},
        "expiry_date": {"value": "2029-12-31", "confidence": 0.92, "check_digit_ok": True},
    }
    l1, l2 = visa_mrz()
    ctx = {
        "mrz_raw": f"{l1}\n{l2}",
        "mrz_fields": {
            "document_code": {"value": "V", "check_digit_ok": None},
            "visa_number": {"value": "AB9876543", "check_digit_ok": True},
            "date_of_birth": {"value": "1985-06-15", "check_digit_ok": True},
            "expiry_date": {"value": "2029-12-31", "check_digit_ok": True},
            "entry_validity": {"value": "multiple", "check_digit_ok": None},
            "stay_duration": {"value": "90 days", "check_digit_ok": True},
        },
    }
    engine = ValidationEngine()
    outcome = engine.validate(fields, doc_type="visa", **ctx)
    assert outcome.is_valid is True
    assert all(c.status != STATUS_FAIL for c in outcome.checks)


def test_visa_bad_stay_duration_fails() -> None:
    fields = {"stay_duration": {"value": "NINETY", "confidence": 0.9}}
    outcome = _run([visa_stay_duration()], fields)
    assert outcome.checks[0].status == STATUS_FAIL


# ---------------------------------------------------------------------------
# Provider + pipeline integration
# ---------------------------------------------------------------------------
def test_provider_contract_and_mock_registry() -> None:
    from app.services.validation.provider import RuleEngineValidationProvider

    provider = RuleEngineValidationProvider()
    rows = [
        {"name": name, "value": fld["value"], "confidence": fld["confidence"],
         "check_digit_ok": fld.get("check_digit_ok")}
        for name, fld in _passport_fields().items()
    ]
    payload = provider.validate(rows, "passport")

    # Step-5 contract keys at the top level:
    assert payload["implemented"] is True
    assert payload["is_valid"] is True
    assert isinstance(payload["checks"], list) and payload["checks"]
    for check in payload["checks"]:
        assert set(check) >= {"field", "status", "message"}
    assert 0.0 <= payload["confidence"] <= 1.0
    # MOCK registry: sample number is not in mock data ⇒ honest miss.
    assert payload["registry"] is None
    assert "MOCK" in payload["validation"]["notes"][1] or payload["validation"]["notes"]


def test_provider_hits_mock_registry(auth_client) -> None:
    from app.db.base import get_session_factory
    from app.db.seed_mock_registry import seed_mock_registry
    from app.services.validation.provider import RuleEngineValidationProvider

    with get_session_factory()() as session:
        seed_mock_registry(session)

    provider = RuleEngineValidationProvider()
    rows = [{"name": "passport_number", "value": "MOCK-P1234567", "confidence": 0.9,
             "check_digit_ok": True}]
    payload = provider.validate(rows, "passport")
    assert payload["registry"] is not None
    assert payload["registry"]["mock"] is True  # honesty stamp present
    assert payload["registry"]["doc_number"] == "MOCK-P1234567"


def test_pipeline_passport_end_to_end(auth_client, tmp_path: Path) -> None:
    sample = Path(__file__).resolve().parents[1] / "data" / "samples" / "sample_passport.png"
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

    stage = stages["document_validation"]
    assert stage["status"] == "ok"
    data = stage["data"]
    assert data["implemented"] is True
    assert data["is_valid"] is True
    checks = data["checks"]
    assert checks, "passport upload must produce real checks"
    for check in checks:
        assert check["status"] in {"PASS", "FAIL", "WARNING"}
        assert check["message"]
    # MRZ-sourced fields must all pass on the synthetic sample.
    fails = [c for c in checks if c["status"] == STATUS_FAIL]
    assert fails == [], f"unexpected failures: {[c['message'] for c in fails]}"
    assert stage["confidence"] is not None and stage["confidence"] > 0.5
    assert stage["human_review_required"] is False


def test_pipeline_garbage_image_flags_review(auth_client, png_bytes: bytes) -> None:
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": ("blank.png", __import__("io").BytesIO(png_bytes), "image/png")},
        data={"doc_type_hint": "passport"},
    )
    run_id = res.json()["run_id"]
    res = auth_client.post("/api/screening/analyze", json={"run_id": run_id})
    stages = {s["stage"]: s for s in res.json()["stages"]}

    stage = stages["document_validation"]
    assert stage["status"] == "ok"
    data = stage["data"]
    # Nothing extractable ⇒ required-field FAILs ⇒ is_valid False + review.
    assert data["is_valid"] is False
    assert stage["human_review_required"] is True
    required_failures = [c for c in data["checks"]
                         if c["status"] == STATUS_FAIL and "missing" in c["message"].lower()]
    assert required_failures


def test_placeholder_provider_still_available(auth_client, monkeypatch) -> None:
    """Switching back to the placeholder stays honest (config seam works)."""
    from app.services import ai_providers

    settings = ai_providers.get_settings()
    monkeypatch.setattr(settings, "ai_validation_provider", "placeholder")
    provider = ai_providers.get_provider("validation")
    assert provider.name.startswith("placeholder-")
    payload = provider.validate([], "passport")
    assert payload["implemented"] is False
