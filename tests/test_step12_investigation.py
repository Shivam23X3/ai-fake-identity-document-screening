"""Step 12 tests: Investigation & Intelligence dashboard.

Layers covered:
- RBAC: officer/reviewer/admin can search+inspect; reviewer cannot fetch
  system-wide aggregates (audit:read fails? no — reviewer HAS audit:read;
  tested instead: unauthenticated 401 + anonymous 401);
- Search: pagination shape, text match on run_id, risk-band filter,
  doc-type filter, date range (from/to inclusive), review filter,
  sort by risk, wildcard-guard on status;
- Stats: aggregate shape (all chart keys), counts add up to total,
  privacy: no PII keys anywhere in the payload, module outcome
  distributions reflect real stored reports;
- Case inspection: whitelisted projection contains risk contributions,
  validation failures, tampering indicators, face result, audit verdict;
  full report only when explicitly requested; 404 unknown, 409
  not-analyzed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import make_png_bytes  # noqa: E402,F401

# Real personal VALUES that must never appear in dashboard payloads
# (fake-person data from the mock registry / sample documents). Field NAMES
# inside validation messages ("required field 'full_name' missing") are
# metadata, not PII, and are allowed.
FORBIDDEN_SUBSTRINGS = (
    "smith", "alice", "l898902c3", "1974-08-12", "utopia", "p<uto",
    "original_path", "probe_image_path", "mrz_raw",
)


def _create_case(auth_client, png_bytes: bytes, doc_type: str = "passport") -> str:
    res = auth_client.post(
        "/api/screening/upload",
        files={"file": ("doc.png", png_bytes, "image/png")},
        data={"doc_type_hint": doc_type},
    )
    assert res.status_code == 201, res.text
    run_id = res.json()["run_id"]
    res = auth_client.post("/api/screening/analyze", json={"run_id": run_id})
    assert res.status_code == 200, res.text
    return run_id


def _forbidden_leak(payload) -> str | None:
    blob = str(payload).lower()
    for needle in FORBIDDEN_SUBSTRINGS:
        if needle in blob:
            return needle
    return None


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
class TestAccessControl:
    def test_search_requires_auth(self, client) -> None:
        assert client.get("/api/investigation/search").status_code == 401

    def test_stats_requires_auth(self, client) -> None:
        assert client.get("/api/investigation/stats").status_code == 401

    def test_case_requires_auth(self, client) -> None:
        assert client.get("/api/investigation/cases/abc").status_code == 401

    def test_reviewer_can_search_and_inspect(self, auth_client, png_bytes) -> None:
        reviewer = _mk_user(auth_client, "inv_reviewer", "REVIEWER")
        run_id = _create_case(auth_client, png_bytes)
        assert auth_client.get("/api/investigation/search", headers=reviewer).status_code == 200
        assert auth_client.get(
            f"/api/investigation/cases/{run_id}", headers=reviewer
        ).status_code == 200

    def test_reviewer_can_read_aggregates(self, auth_client) -> None:
        # reviewers hold audit:read → charts are visible to them too
        reviewer = _mk_user(auth_client, "inv_stats_reviewer", "REVIEWER")
        assert auth_client.get("/api/investigation/stats", headers=reviewer).status_code == 200


def _mk_user(auth_client, username: str, role: str) -> dict:
    res = auth_client.post(
        "/api/auth/users",
        json={"username": username, "password": "Whatever#Pass123", "role": role},
    )
    assert res.status_code == 201, res.text
    login = auth_client.post(
        "/api/auth/login", json={"username": username, "password": "Whatever#Pass123"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
class TestSearch:
    def test_shape_and_pagination(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes)
        res = auth_client.get("/api/investigation/search?limit=1&offset=0")
        assert res.status_code == 200
        body = res.json()
        assert {"items", "total", "limit", "offset", "count", "disclaimer"} <= set(body)
        assert body["limit"] == 1 and body["count"] <= 1
        if body["items"]:
            row = body["items"][0]
            assert {"run_id", "risk_band", "risk_score", "created_at",
                    "human_review_required"} <= set(row)

    def test_text_query_matches_run_id(self, auth_client, png_bytes) -> None:
        run_id = _create_case(auth_client, png_bytes)
        res = auth_client.get(f"/api/investigation/search?q={run_id[:16]}")
        assert res.status_code == 200
        assert any(item["run_id"] == run_id for item in res.json()["items"])

    def test_risk_band_filter(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes)
        res = auth_client.get("/api/investigation/search?risk_band=low")
        assert res.status_code == 200
        assert all(i["risk_band"] in (None, "low") for i in res.json()["items"])

    def test_invalid_band_rejected(self, auth_client) -> None:
        assert auth_client.get(
            "/api/investigation/search?risk_band=ultra"
        ).status_code == 422

    def test_doc_type_filter(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes, doc_type="visa")
        res = auth_client.get("/api/investigation/search?doc_type=visa")
        assert res.status_code == 200
        items = res.json()["items"]
        assert items
        assert all("visa" in (i["doc_type_detected"] or i["doc_type_hint"] or "") for i in items)

    def test_date_range_filters(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes)
        today = __import__("datetime").date.today().isoformat()
        res = auth_client.get(f"/api/investigation/search?date_from={today}&date_to={today}")
        assert res.status_code == 200
        assert res.json()["total"] >= 1
        # an impossible historical window returns nothing
        empty = auth_client.get(
            "/api/investigation/search?date_from=2001-01-01&date_to=2001-01-02"
        )
        assert empty.json()["total"] == 0

    def test_review_filter(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes)
        res = auth_client.get("/api/investigation/search?review_required=true")
        assert res.status_code == 200
        assert all(i["human_review_required"] for i in res.json()["items"])

    def test_sort_by_risk(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes)
        res = auth_client.get("/api/investigation/search?sort=risk_desc&limit=50")
        assert res.status_code == 200
        scores = [i["risk_score"] for i in res.json()["items"] if i["risk_score"] is not None]
        assert scores == sorted(scores, reverse=True)

    def test_status_wildcard_guard(self, auth_client) -> None:
        assert auth_client.get(
            "/api/investigation/search?status=%25"
        ).status_code == 422


# ---------------------------------------------------------------------------
# Stats (visualizations)
# ---------------------------------------------------------------------------
class TestStats:
    def test_aggregate_shape(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes)
        res = auth_client.get("/api/investigation/stats")
        assert res.status_code == 200
        body = res.json()
        for key in (
            "total", "screenings_per_day", "risk_distribution",
            "doc_type_distribution", "validation", "tampering", "face",
            "human_review", "status", "integrity", "privacy_note",
        ):
            assert key in body, f"missing aggregate {key}"
        spd = body["screenings_per_day"]
        assert len(spd["days"]) == 14
        assert all("day" in d and "count" in d for d in spd["days"])

    def test_counts_sum_to_total(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes, doc_type="passport")
        _create_case(auth_client, png_bytes, doc_type="visa")
        body = auth_client.get("/api/investigation/stats").json()
        total = body["total"]
        assert total >= 2
        assert sum(body["risk_distribution"].values()) == total
        assert sum(body["doc_type_distribution"].values()) == total
        assert sum(body["validation"].values()) == total
        assert sum(body["tampering"].values()) == total
        assert sum(body["face"].values()) == total
        assert sum(body["human_review"].values()) == total

    def test_module_outcomes_reflect_report(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes)
        body = auth_client.get("/api/investigation/stats").json()
        # the fresh analyze ran all real engines → not all-unknown
        assert body["validation"]["unknown"] < body["total"]
        assert body["tampering"]["unknown"] < body["total"]

    def test_no_pii_in_aggregates(self, auth_client, png_bytes) -> None:
        _create_case(auth_client, png_bytes)
        body = auth_client.get("/api/investigation/stats").json()
        assert _forbidden_leak(body) is None

    def test_filters_reflected_in_window(self, auth_client) -> None:
        body = auth_client.get(
            "/api/investigation/stats?risk_band=high&doc_type=passport"
        ).json()
        assert body["window"]["risk_band"] == "high"
        assert body["window"]["doc_type"] == "passport"


# ---------------------------------------------------------------------------
# Case inspection
# ---------------------------------------------------------------------------
class TestCaseInspection:
    def test_case_projection_complete(self, auth_client, png_bytes) -> None:
        run_id = _create_case(auth_client, png_bytes)
        res = auth_client.get(f"/api/investigation/cases/{run_id}")
        assert res.status_code == 200
        body = res.json()
        # risk factors
        assert isinstance(body["risk"]["contributions"], list)
        assert isinstance(body["risk"]["reasons"], list)
        # validation failures
        assert isinstance(body["validation"]["checks"], list)
        assert {"is_valid", "failures", "warnings"} <= set(body["validation"])
        # tampering indicators
        assert "verdict" in body["tampering"]
        assert isinstance(body["tampering"]["indicators"], list)
        # face result
        assert "match_status" in body["face"]
        # blockchain integrity verdict
        assert "verify" in body["audit"]
        assert body["audit"]["verify"] is not None
        assert {"verified", "on_ledger", "conclusion"} <= set(body["audit"]["verify"])
        assert "raw_report" not in body

    def test_full_report_opt_in(self, auth_client, png_bytes) -> None:
        run_id = _create_case(auth_client, png_bytes)
        body = auth_client.get(
            f"/api/investigation/cases/{run_id}?include_full_report=true"
        ).json()
        assert "raw_report" in body

    def test_projection_carries_no_raw_ocr(self, auth_client, png_bytes) -> None:
        run_id = _create_case(auth_client, png_bytes)
        body = auth_client.get(f"/api/investigation/cases/{run_id}").json()
        assert _forbidden_leak(body) is None
        # the ocr block (names etc.) must not be part of the projection
        assert "ocr" not in body
        # validation checks expose (field, status, message) — no raw values
        for chk in body["validation"]["checks"]:
            assert set(chk) == {"field", "status", "message"}
        # tampering indicators expose type/severity/confidence/note only
        for ind in body["tampering"]["indicators"]:
            assert set(ind) <= {"type", "severity", "confidence", "note"}

    def test_unknown_run_404(self, auth_client) -> None:
        res = auth_client.get(f"/api/investigation/cases/{'f' * 32}")
        assert res.status_code == 404

    def test_not_analyzed_409(self, auth_client, png_bytes) -> None:
        res = auth_client.post(
            "/api/screening/upload",
            files={"file": ("doc.png", png_bytes, "image/png")},
            data={"doc_type_hint": "passport"},
        )
        run_id = res.json()["run_id"]
        res = auth_client.get(f"/api/investigation/cases/{run_id}")
        assert res.status_code == 409
