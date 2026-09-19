# Testing Strategy (Step 13)

Complete, layered test strategy for the AI-Based Fake Identity & Document
Screening System. **All test documents are synthetic SPECIMEN artifacts
(fabricated UTO / UTOSLAND identities, machine-printed, visibly bannered
"SPECIMEN — NO VALUE"). No real person's identity document is used, copied
or imitated anywhere in this repository.**

---

## 1. Test pyramid — five layers

| Layer | What it proves | How it runs | Speed |
|---|---|---|---|
| **Unit** | One function/module is correct in isolation (MRZ check digits, fusion math, hashing, validators) | In-process imports, no HTTP | ms each |
| **Integration** | Modules cooperate through real engines (preprocess → OCR → validation → tampering → face → risk) | Real CV/OCR engines, temp DB | seconds |
| **API** | Endpoint contracts: status codes, envelope shape, RBAC, rate limits, error paths | FastAPI `TestClient` (no network) | fast |
| **Security** | Step-11 controls actually hold: authn/z, JWT lifecycle, headers, upload hardening, encryption at rest | `TestClient` + direct crypto checks | fast |
| **End-to-end** | The full user journey: upload → analyze → audit log → ledger verify → investigation search | `TestClient` against the full app with lifespan | tens of seconds |

Markers (`pytest.ini`): `unit`, `integration`, `api`, `security`, `e2e`, `slow`.

---

## 2. Coverage matrix — 12 required areas

| # | Area | Suite(s) | Highlights |
|---|---|---|---|
| 1 | **OCR** | `tests/test_step4_ocr.py`, `test_step13_failures.py::TestBlurryImage/TestOcrFailure` | ICAO-9303 check-digit vectors, MRZ repair, field confidence; blur degradation; blank-page failure |
| 2 | **Document validation** | `tests/test_step5_validation.py` | Per-country rulesets, cross-consistency, MOCK registry lookups, PASS/FAIL/WARNING |
| 3 | **Tampering detection** | `tests/test_step6_tampering.py`, `test_step13_failures.py::TestTamperingSignals` | ELA, noise, JPEG grid, copy-move, metadata; splice specimen → honest verdicts |
| 4 | **Face verification** | `tests/test_step7_face.py`, `test_step13_failures.py::TestFaceCases` | YuNet/SFace quality gates, same-image MATCH, no-probe honesty, multi-face refusal |
| 5 | **Risk scoring** | `tests/test_step8_risk.py`, missing/expiry contributions in `test_step13_failures.py` | Deterministic weighted rules, band config, explainable contributions |
| 6 | **API endpoints** | `tests/test_step2_api.py`, `test_step10_audit_ledger.py`, `test_step12_investigation.py` | Contracts, filters, pagination, 401/403/404/409/413/415/422/429 paths |
| 7 | **Database** | `tests/test_step1_skeleton.py` + every suite (isolated SQLite per run) | Schema, migrations (`_sync_sqlite_columns`), encryption at rest round-trips |
| 8 | **Blockchain audit logging** | `tests/test_step9_pipeline.py`, `test_step10_audit_ledger.py`, `test_step13_failures.py::TestBlockchainUnavailable` | Canonical records, PII guard, hash-only anchoring, honest pending/offline states |
| 9 | **Authentication** | `tests/test_step11_security.py` | Argon2id, JWT lifecycle + revocation, lockout, uniform login errors |
| 10 | **File uploads** | `tests/test_step11_security.py::TestUploadHardening`, `test_step13_failures.py::TestCorruptAndUnsupportedFiles` | Magic bytes, size/pixel caps, re-encode, polyglot/bomb rejection |
| 11 | **Frontend** | `scripts/ui_check*.mjs` (headless Chrome over CDP) + `npm run build` typecheck | Login → upload → panels render; real DOM assertions; production build compiles |
| 12 | **End-to-end screening** | `tests/test_step13_failures.py::TestEndToEndSpecimen` + suites 9/10/12 | Full journey incl. ledger verify + investigation search |

---

## 3. Failure-case matrix (all implemented and passing)

| Failure case | Test | Expected honest outcome |
|---|---|---|
| Blurry image | `TestBlurryImage` | Pipeline completes; OCR confidence drops vs sharp control; human review forced |
| Rotated image | `TestRotatedImage` | Preprocess `ok` (deskew applied or flagged); review routed |
| Missing fields | `TestMissingFields` | "Required field(s) missing" risk reasons/contributions; review routed |
| Expired document | `TestExpiredDocument` | "Document EXPIRED …" reason in risk assessment |
| Corrupted file | `TestCorruptAndUnsupportedFiles::test_truncated_png_rejected` | `415` at upload — never persisted |
| Unsupported file | `…::test_unsupported_magic_rejected`, `…::test_disallowed_extension_rejected` | `415` (magic-byte / extension gate) |
| Oversized pixel claim | `…::test_oversized_pixel_claim_rejected` | `415` (50 MP decode budget) |
| No face | `TestFaceCases::test_faceless_document_no_match_verdict` | `face_detected_document=false` → INCONCLUSIVE, no fabricated verdict |
| Multiple faces | `…::test_multiple_faces_flagged_not_verified` | No MATCH possible; review routed |
| Suspected tampering | `TestTamperingSignals` | Forensic verdict reflected in risk reasons; never "proof" |
| OCR failure | `TestOcrFailure` | No crash; unavailable signals add explained risk points |
| Blockchain unavailable | `TestBlockchainUnavailable` | Anchors stay `pending`; verify says NOT ON LEDGER / UNAVAILABLE — never fake "verified" |

---

## 4. Specimen documents (legal + technical policy)

- Generated **in code** (`tests/synthetic_docs.py`, `scripts/make_samples.py`)
  from fabricated identities of fictional countries (UTO / UTOSLAND).
- Every rendered specimen carries a visible **"SPECIMEN — NO VALUE"** banner.
- MRZ check digits are mathematically valid (ICAO 9303 worked-example style)
  so the OCR pipeline is exercised for real — but no data maps to any real
  person or real issuing authority.
- Regenerate on-disk samples any time (optional; tests build them in memory):

```bash
.venv/Scripts/python scripts/make_samples.py
# → data/samples/sample_passport.png, data/samples/sample_visa.png
```

Face models (public ONNX research models, no personal data):

```bash
.venv/Scripts/python scripts/get_face_models.py
```

---

## 5. Commands & expected outputs

All commands run from the repository root (Windows venv path shown; on
POSIX use `.venv/bin/python`).

### 5.1 Everything

```bash
.venv/Scripts/python -m pytest -q
```

Expected (Step 13 complete):

```
283 passed, ~114 warnings in ~4-5 min
```

(Warnings are library deprecations + the deliberate DecompressionBomb
probe; they are collected in §7 and none indicate a test failure.)

### 5.2 Per-area commands

```bash
# 1. OCR (unit + API)
.venv/Scripts/python -m pytest tests/test_step4_ocr.py -q
#   → "22 passed"  (check-digit vectors, MRZ repair, field extraction)

# 2. Document validation
.venv/Scripts/python -m pytest tests/test_step5_validation.py -q
#   → all passed  (rulesets, cross-consistency, registry)

# 3. Tampering detection
.venv/Scripts/python -m pytest tests/test_step6_tampering.py -q
#   → all passed  (detectors on pristine vs edited synthetic documents)

# 4. Face verification
.venv/Scripts/python -m pytest tests/test_step7_face.py -q
#   → all passed  (quality gates, MATCH on same image, honest INCONCLUSIVE)

# 5. Risk scoring
.venv/Scripts/python -m pytest tests/test_step8_risk.py -q
#   → all passed  (deterministic fusion, bands, explainability)

# 6. API endpoints
.venv/Scripts/python -m pytest tests/test_step2_api.py tests/test_step10_audit_ledger.py tests/test_step12_investigation.py -q
#   → all passed  (contracts, RBAC, filters, ledger + investigation)

# 7. Database / skeleton
.venv/Scripts/python -m pytest tests/test_step1_skeleton.py -q
#   → all passed

# 8. Blockchain audit logging
.venv/Scripts/python -m pytest tests/test_step9_pipeline.py tests/test_step10_audit_ledger.py tests/test_step13_failures.py::TestBlockchainUnavailable -q
#   → all passed  (hash-only anchoring; offline ⇒ pending, honestly)

# 9. Authentication / security
.venv/Scripts/python -m pytest tests/test_step11_security.py -q
#   → "31 passed"  (Argon2id, JWT, RBAC, headers, hardening, encryption)

# 10. File uploads (hardening + failure gates)
.venv/Scripts/python -m pytest tests/test_step11_security.py::TestUploadHardening tests/test_step13_failures.py::TestCorruptAndUnsupportedFiles -q
#   → all passed

# 11. Failure modes (the Step-13 suite)
.venv/Scripts/python -m pytest tests/test_step13_failures.py -q
#   → "20 passed in ~1 min"  (blur/rotate/missing/expired/corrupt/unsupported/
#      no-face/multi-face/tampering/OCR-failure/chain-offline)

# 12. End-to-end only
.venv/Scripts/python -m pytest -m e2e -q
#   → "2 passed"  (passport + visa specimen full journeys)
```

### 5.3 By layer / speed

```bash
.venv/Scripts/python -m pytest -m unit -q          # fast unit layer only
.venv/Scripts/python -m pytest -m "not slow" -q    # everything quick (CI default)
.venv/Scripts/python -m pytest -m security -q      # security controls
```

Expected: selection works via `pytest.ini` markers; `-m "not slow"` collects
the full suite minus any future slow-marked tests.

### 5.4 Frontend

```bash
cd frontend && npm run build
# Expected:
#   ✓ 27 modules transformed.
#   dist/assets/index-*.js   ~277 kB │ gzip ~84 kB
#   ✓ built in <1 s
# (tsc -b runs first — any type error fails the build)

cd frontend && npm run lint
# Expected: no oxlint errors

# Live UI drive (needs backend on :8000 + vite dev server on :5173):
cd frontend && npm run dev           # terminal 1
.venv/Scripts/python -m uvicorn app.main:app --port 8000   # terminal 2
node scripts/ui_check.mjs --screenshot       # terminal 3
# Expected output ends with:
#   PASS  login screen rendered
#   PASS  logged in, dashboard rendered (backend reachable)
#   PASS  upload submitted: true
#   PASS  analysis finished, validation checks rendered
#   UI VERIFICATION: ALL CHECKS PASSED
#   screenshot saved: logs/ui_validation_panel.png
```

### 5.5 Full-stack smoke (manual demo rehearsal)

```bash
# Optional: bring up the local EVM chain for on-chain anchoring
cd blockchain && npx hardhat node                 # terminal 1
cd blockchain && npm run deploy                   # terminal 2 (writes deployed.json)

.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000   # terminal 3
cd frontend && npm run dev                        # terminal 4

curl http://localhost:8000/health
# → {"success":true,"status":"healthy", ...}
curl -F "file=@data/samples/sample_passport.png;type=image/png" \
     -F "doc_type_hint=passport" http://localhost:8000/api/screening/upload
# → 201 {"run_id":"…","file":{"sha256":"…",…}}
# then POST /api/screening/analyze with {"run_id":"…"} → all 6 stages ok
```

---

## 6. Test-isolation & environment rules

- `tests/conftest.py` pins a **disposable SQLite DB + temp dirs** per run
  (`APP_DB_URL=sqlite:///...test.db`, `APP_ENV=test`), so tests never touch
  `data/app.db`.
- Deterministic test crypto: fixed `APP_ENCRYPTION_KEY`, bootstrap admin
  from env (`testadmin`), rate limits raised to `1000/minute` except in the
  dedicated 429 test.
- `auth_client` fixture injects an admin Bearer token into every request so
  suites stay readable; RBAC tests deliberately override headers.
- Blockchain tests are **node-optional**: anchors honestly stay `pending`
  when no Hardhat node is running — the assertion is on honesty, not on
  chain liveness.

## 7. Known warnings (all benign, reviewed)

| Warning | Why it appears | Action |
|---|---|---|
| `InsecureKeyLengthWarning` (24-byte HMAC key) | Test JWT fixtures use a short dev secret | Real deployments use `APP_SECRET_KEY` ≥ 32 chars (enforced in prod boot) |
| `DecompressionBombWarning` | The upload test *deliberately* exceeds the pixel budget to prove the cap fires | Intentional |
| `StarletteDeprecationWarning` / `anyio` / `websockets.legacy` / `passlib argon2.__version__` | Upstream library deprecations | Track upstream; no runtime impact |

## 8. What is deliberately NOT tested

- Anything against a real government registry (does not exist by design —
  MOCK_MODE only).
- Real biometric identification against a population (the system is 1:1
  verification only, by design).
- Multi-node / Redis-backed revocation (single-node prototype; the
  interface is the tested two-function denylist).
