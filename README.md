# AI-Based Fake Identity & Document Screening System

SIH 2026 project. AI-**assisted** decision-support for authorized
border/security personnel to screen identity and travel documents
(passport, visa, national ID, driving license, permits).

> **This system does not make decisions.** Every output is advisory,
> carries a confidence score where applicable, and high-risk results
> are always routed to human review.

## Pipeline

```
Input → Preprocessing → OCR → Document Validation → Tampering Detection
      → Face Verification → Risk Assessment → Screening Result → Audit Log
```

## Honest-by-design commitments

- No fake integrations with government databases; mock data lives in
  `data/mock_registry` and is clearly marked as demo data.
- No claims of 100% forgery detection; probabilistic outputs with
  confidence scores only.
- Final decisions always rest with authorized human personnel.

## Setup (Windows, PowerShell/bash)

Requires Python 3.12 (the CV/OCR stack does not fully support 3.14 yet).

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

## Run

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

- Swagger UI: http://localhost:8000/docs
- Health: http://localhost:8000/health
- Pipeline info: http://localhost:8000/pipeline/info

## Test

```bash
.venv/Scripts/python -m pytest -q
# → 300 passed (see TESTING.md for the complete Step-13 strategy,
#   per-area commands and expected outputs)
```

## Security (Step 11)

Every decision is explainable — the short version:

| Control | Choice | Why |
|---|---|---|
| Passwords | Argon2id (passlib) | memory-hard PHC winner; bcrypt truncates at 72 bytes |
| Sessions | JWT HS256, 30-min TTL, `jti` denylist revocation | stateless but revocable; short TTL bounds replay |
| Bootstrap admin | env-only credentials, 60-second token + forced rotation | no hardcoded defaults; bootstrap can't become a standing credential |
| RBAC | ADMIN / SECURITY_OFFICER / REVIEWER, deny-by-default matrix | officers run screening, reviewers inspect flagged cases, admins manage |
| Login errors | one generic message + dummy-hash timing equalizer | no username enumeration, no timing oracle |
| Brute force | lockout after 5 failures / 15 min (behind per-IP limits) | survives distributed attempts better than rate limits alone |
| Uploads | magic-byte + extension + size cap + 50 MP pixel budget + Pillow re-encode | blocks polyglots, decompression bombs, EXIF payload smuggling |
| Rate limits | 10/min auth, 30/min upload, 20/min analyze, 120/min default | heaviest limits on credential + CPU-heavy routes |
| Headers | nosniff, DENY, CSP, no-referrer, COOP/CORP, `Cache-Control: no-store`, HSTS on https | results/PII must never be cached or framed |
| CORS | explicit origin list, credentials off (Bearer tokens) | header-borne tokens make CSRF structural, not mitigated |
| Encryption at rest | Fernet (AES-CBC + HMAC) on `report_json`, key via `APP_ENCRYPTION_KEY` | OCR fields/MRZ/names are the PII-dense artifact; stolen DB file alone is useless; REQUIRED in prod |
| In transit | HTTPS-aware (HSTS when TLS); behind reverse proxy in prod | prototype dev runs plain HTTP honestly |
| Cleanup | upload dirs deleted after `APP_UPLOAD_RETENTION_HOURS` (24 h default) | data minimization for document images |
| Errors | uniform envelope; internals only in dev; secrets never logged | no info leakage from API or logs |

```bash
### Environment Variables

Create a `.env` file locally and configure the required environment variables:

```env
APP_SECRET_KEY=<your-secret-key>
APP_ENCRYPTION_KEY=<your-encryption-key>
APP_BOOTSTRAP_ADMIN_PASSWORD=<your-admin-password>
APP_ENV=prod
```

Roles: **SECURITY_OFFICER** runs screenings (upload/analyze/audit-log),
**REVIEWER** inspects results/flagged cases (read-only), **ADMIN** manages
users, roles and configuration (`/pipeline/info` is admin-only).

## Investigation & Intelligence dashboard (Step 12)

The **Investigation & Intelligence** tab gives authorized users a live
analysis view over stored screenings:

- **Search & filters** — text search (run id / file sha256 / doc type only;
  PII fields are deliberately NOT searchable), risk band, document type,
  UTC date range, pipeline status, human-review-only toggle, sorted
  results with pagination.
- **Visualizations** (dependency-free SVG, aggregate `(label, count)`
  data only): screenings per day (14-day window), risk distribution,
  document-type distribution, validation outcomes, tampering statistics,
  face-verification outcomes, human-review queue and blockchain anchor
  posture.
- **Case inspector** — one click on any row opens the whitelisted case
  projection: risk contributions + explainability reasons, validation
  failures/checks, tampering indicators (type/severity/note only), face
  verdict (similarity + confidence), and the per-case blockchain integrity
  verdict (recomputed hash vs ledger commitment).
- **Privacy by construction** — search rows, charts and the case projection
  never include names, document numbers, MRZ text, images or biometric
  data; aggregates are computed server-side over stored metadata. The full
  stored report remains available to authorized roles only via
  `GET /api/screening/{run_id}` (opt-in `include_full_report=true` on the
  case endpoint).
- RBAC: search + case inspection require `screening:read` (officer /
  reviewer / admin); aggregates require `audit:read`.

## Blockchain audit layer (Steps 9-10)

```bash
# Terminal 1: local permissioned EVM chain
cd blockchain && npx hardhat node

# Terminal 2: deploy the anchor contract (writes deployed.json + abi/)
cd blockchain && npm run deploy

# Terminal 3: backend (APP_LEDGER_BACKEND=auto picks the chain up;
# with the node offline everything still works — anchors stay 'pending')
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

The chain stores HASHES + statuses only — never document images, biometric
data or personal fields. `GET /api/audit/verify/{screening_id}` recomputes
the result hash from the stored report and reports UNCHANGED / CHANGED /
NOT ON LEDGER / UNAVAILABLE honestly.


## SIH demonstration mode (Step 14)

The **🎬 SIH Demo** tab (and `POST /api/demo/run/{case_id}`) runs four
scripted scenarios through the **real** pipeline on synthetic SPECIMEN
fixtures generated by `scripts/make_demo_fixtures.py`:

| Case | Scenario | Expected outcome |
|------|----------|------------------|
| `case1_valid` | genuine passport + matching live photo | OCR ok, validation passes, no tampering, face MATCH, LOW risk |
| `case2_tampered` | spliced portrait + altered printed DOB | tampering indicators, increased risk, human review |
| `case3_identity_mismatch` | genuine passport + a DIFFERENT presenter | validation passes, face NO_MATCH, HIGH risk |
| `case4_low_quality` | blurred, low-quality scan | OCR fails honestly, human review |

```bash
# (re)generate the fixture set (deterministic; calibrates against YuNet)
.venv/Scripts/python scripts/make_demo_fixtures.py --check-faces

# terminal rehearsal of all four cases (honest checkpoint tables)
.venv/Scripts/python scripts/demo_run_cases.py
```

Honesty guarantees (enforced by tests in `tests/test_step14_demo.py`):

- every demo response is labeled **DEMONSTRATION / SIMULATED** at the top
  level, on the panel, and in the disclaimer;
- the stored/audited report keeps its canonical shape — demo markers are
  attached AFTER the audit hash is computed, so `verify` stays reproducible;
- demo run ids are clearly identifiable (`demo_case..._xxxxxxxx`);
- expected-vs-actual checkpoints are computed from the REAL result and shown
  as-is (a failed checkpoint renders as failed — nothing is papered over);
- registry lookups stay MOCK-stamped; **no government verification is
  simulated anywhere**.


## API (Step 2)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/screening/upload` | Upload document image/PDF (`file` + `doc_type_hint` form field) |
| POST | `/api/screening/analyze` | Run pipeline for a stored run (`{"run_id": "..."}`) |
| GET  | `/api/screening/{run_id}` | Full report for one run |
| GET  | `/api/screening/history?limit=&offset=` | Paginated screening history |
| POST | `/api/audit/log` | Build + log the canonical audit record on the ledger (`{"run_id": "..."}`) |
| GET  | `/api/audit/{screening_id}` | Canonical audit record + ledger state + event trail |
| GET  | `/api/audit/verify/{screening_id}` | Recompute the result hash; detect tampering |
| GET  | `/api/audit/status` | Active ledger backend diagnostics |
| GET  | `/api/investigation/search` | Filtered case search (`q`, `risk_band`, `doc_type`, `date_from/to`, `review_required`, `status`, sort, pagination) |
| GET  | `/api/investigation/stats` | Aggregate chart statistics over the same filtered corpus |
| GET  | `/api/investigation/cases/{run_id}` | Case inspector: risk factors, validation failures, tampering indicators, face result + blockchain integrity verdict |
| GET  | `/health` · `/pipeline/info` | Health check / stage + AI-provider status |

Swagger UI: **http://localhost:8000/docs** — all endpoints testable from the browser.

## Verify the backend works (Step 2)

```bash
# 1. Unit/integration tests (59)
.venv/Scripts/python -m pytest -q

# 2. Start the server
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# 3. Smoke-test the full flow
curl http://localhost:8000/health
curl -F "file=@data/samples/sample_synthetic.png;type=image/png" \
     -F "doc_type_hint=passport" http://localhost:8000/api/screening/upload
# → copy run_id from the response, then:
curl -X POST -H "Content-Type: application/json" \
     -d '{"run_id":"<RUN_ID>"}' http://localhost:8000/api/screening/analyze
curl http://localhost:8000/api/screening/history
```

Expected: upload returns `201` with a `run_id` + file sha256; analyze returns all
6 stages — preprocess/OCR/validation/tampering/face/risk all run real engines
(`ok`; face reports an honest INCONCLUSIVE when no probe image was uploaded) —
and the run appears in history as `pending_review` when anything requires
human verification.

- [x] Step 1 — Skeleton: FastAPI + modular pipeline core (6 stub stages)
- [x] Step 2 — Backend foundation: DB (SQLAlchemy), 4 REST endpoints, config,
      error handling, PII-safe logging, CORS, validation, AI provider seams
- [x] Step 2b — Image preprocessing (deskew, perspective, denoise, contrast,
      quality metrics — `app/services/ocr/preprocessing.py`)
- [x] Step 3/4 — OCR extraction module (RapidOCR with PaddleOCR models via
      ONNX + ICAO 9303 MRZ parser with check-digit repair, field extraction,
      per-field confidence, honest review routing — `app/services/ocr/`)
- [x] Step 5 — Document validation rule engine (configurable rulesets per
      doc type + country, passport/visa rules, MRZ cross-consistency,
      MOCK registry lookup, `PASS/FAIL/WARNING` checks —
      `app/services/validation/`; generic simplified rules, NOT any
      country's official rules)
- [x] Step 6 — Tampering detection (self-contained forensics module:
      metadata forensics, quality-aware ELA, noise inconsistency,
      JPEG-grid compression analysis, copy-move with shift verification +
      periodic-layout suppression, photo/stamp region cues, optional
      trained-model seam; signal fusion → 0-100 risk + honest verdicts
      `no_obvious_manipulation | suspicious | likely_manipulated |
      inconclusive` — `app/services/tampering/`)
- [x] Step 6 — Face verification (YuNet detection + SFace 1:1 embeddings,
      quality/pose/occlusion safeguards, honest INCONCLUSIVE routing,
      probe-image upload + preview endpoint — `app/services/face/`)
- [x] Step 7 — Risk assessment (deterministic explainable weighted-rules
      fusion of ALL signals — OCR confidence, validation, tampering, face
      1:1, expiry, missing fields, suspicious field patterns, metadata
      anomalies — → 0-100 risk score + configurable LOW/MEDIUM/HIGH/CRITICAL
      bands (defaults 24/49/74, `APP_RISK_BAND_*`) + human-readable reasons
      + additive human-review routing — `app/services/risk/`)
- [x] Step 8 — React dashboard (upload → per-stage panels → consolidated
      result + audit view; headless-Chrome UI checks in `scripts/ui_check*.mjs`)
- [x] Step 9 — Complete pipeline orchestration + blockchain audit log:
      `app/services/orchestrator.py` coordinates preprocess → OCR →
      validation → tampering → face → risk with failure isolation, per-stage
      timing/confidence and the consolidated Step-9 result (screening_id,
      per-module payloads, final_status, processing_time_ms);
      `app/services/audit/` commits sha256(canonical report) to
      `audit_events` and anchors the hash on a local EVM chain via web3.py
      (`blockchain/` — AuditAnchor.sol + Hardhat; anchors honestly stay
      `pending` when the node is offline; hashes only, never PII);
      endpoints: `GET /api/screening/{run_id}/result`,
      `GET /api/screening/{run_id}/audit`
- [x] Step 10 — Blockchain audit logging: canonical audit records
      (screening_id, timestamp, result hash, risk score, validation /
      tampering / face status, system/version id — NEVER images, biometrics
      or PII) hashed and committed to a permissioned ledger behind the
      `LedgerBackend` interface (`app/services/audit/ledger.py`: local mock
      ledger, EVM AuditAnchor adapter, `APP_LEDGER_BACKEND=local|evm|auto`;
      Hyperledger Fabric = implement the same 3 methods);
      endpoints: `POST /api/audit/log`, `GET /api/audit/{screening_id}`,
      `GET /api/audit/verify/{screening_id}` (recomputes the hash and flags
      any post-hoc change as RECORD CHANGED), `GET /api/audit/status`;
      dashboard shows the blockchain verification verdict per screening.
- [ ] Step 10b — Integration + docs (final pass)
- [x] Step 13 — Testing strategy: layered pyramid (unit / integration /
      API / security / e2e, markers in `pytest.ini`) over all 12 areas
      (OCR, validation, tampering, face, risk, API, DB, blockchain audit,
      auth, uploads, frontend, end-to-end) + a dedicated failure-mode
      suite (`tests/test_step13_failures.py`: blur, rotation, missing
      fields, expired document, corrupted/unsupported/oversized files,
      no face, multiple faces, suspected tampering, OCR failure,
      blockchain unavailable); synthetic SPECIMEN documents only
      (`tests/synthetic_docs.py`, bannered, fabricated UTO/UTOSLAND
      identities — never real documents); strategy + commands + expected
      outputs in `TESTING.md`
- [x] Step 12 — Investigation & Intelligence dashboard: filtered search
      (`/api/investigation/search`: risk band, doc type, date range,
      review-flag, status, sort + pagination), aggregate visualizations
      (`/api/investigation/stats`: screenings/day, risk distribution, doc
      types, validation/tampering/face outcomes, human-review queue,
      ledger integrity) and the case inspector
      (`/api/investigation/cases/{run_id}`: risk factors, validation
      failures, tampering indicators, face result + blockchain verify
      verdict); PII-safe by construction — metadata-only search, aggregate
      charts, whitelisted case projection (`app/services/investigation_service.py`,
      `app/api/routes_investigation.py`, `frontend/src/components/Investigation.tsx`)
- [x] Step 14 — SIH demonstration mode: four scripted cases (`case1_valid`,
      `case2_tampered`, `case3_identity_mismatch`, `case4_low_quality`)
      run through the REAL pipeline on synthetic SPECIMEN fixtures
      (`scripts/make_demo_fixtures.py`, deterministic, calibrated against
      YuNet); `POST /api/demo/run/{case_id}` + catalog endpoint
      (`app/services/demo_mode.py`, `app/api/routes_demo.py`), 🎬 SIH Demo
      tab with expected-vs-actual checkpoints rendered as-is
      (`frontend/src/components/DemoMode.tsx`), CLI rehearsal
      (`scripts/demo_run_cases.py`); every response labeled
      DEMONSTRATION/SIMULATED, demo markers attached AFTER the audit hash
      so `verify` stays reproducible, registry lookups stay MOCK-stamped,
      no government verification simulated anywhere; honesty guarantees
      enforced by `tests/test_step14_demo.py`

