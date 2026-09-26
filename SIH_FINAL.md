# SIH 2026 — Final Build, Architecture & Presentation Guide

Single reference for the final deliverable: architecture, setup, startup,
testing, the scripted demo, and troubleshooting. `ARCHITECTURE.md`,
`TESTING.md` and `DEPLOY.md` remain available for depth.

Status: **300/300 backend tests pass · frontend TypeScript build clean.**

---

## 1. Final architecture

```
                ┌────────────────────────────────────────────────────────┐
                │                      Browser (SPA)                     │
                │  React + TypeScript dashboard (Login / New Screening / │
                │  Detail / Investigation / 🎬 SIH Demo)                  │
                └───────────────────────────┬────────────────────────────┘
                                            │ HTTPS 443 (HTTP 80 demo-IP)
                        ┌───────────────────▼───────────────────┐
                        │             caddy (public)            │
                        │  static frontend + same-origin /api,  │
                        │  /health, /pipeline reverse proxy     │
                        └───────────────────┬───────────────────┘
                                            │ internal docker network
                ┌───────────────────────────▼───────────────────────────┐
                │                 FastAPI api (uvicorn)                 │
                │  auth (Argon2id + JWT + RBAC + lockout + rate limits) │
                │  upload hardening (magic bytes, 50 MP budget, re-enc) │
                │  Orchestrator (Step 9) ── failure-isolated pipeline:  │
                │   preprocess → OCR → validation → tampering → face    │
                │   → risk → consolidated result → audit trail          │
                │  Fernet encryption at rest for report_json            │
                └───────┬───────────────────────────────┬───────────────┘
                        │ hashes only                   │ SQLite (private
        ┌───────────────▼──────────────┐  │ volume api_data) + uploads
        │  hardhat: EVM chain (local)  │◄─┘ audit_events + mock registry
        │  AuditAnchor commitments     │
        │  internal only, no host port │   chain-deploy: one-shot contract
        └──────────────────────────────┘   deploy → deployed.json + ABI
```

Key properties:

- **Every AI stage is a pluggable provider** (`app/services/ai_providers.py`).
  A capability that is unavailable degrades to an honest
  `not_implemented` + forced human review — never a fabricated result.
- **The orchestrator never makes a decision.** `final_status` is advisory;
  `human_review_required` can only be forced ON.
- **The ledger receives hashes + statuses only.** Images, biometrics and
  personal fields never leave the local DB (`validate_no_pii` guards it).
- **Failures degrade forward.** A crashed stage marks itself `error`, adds
  risk points, and the run continues (stage policy `skip`).

---

## 2. Complete folder structure

```
├── app/                        FastAPI backend
│   ├── main.py                 app factory, lifespan, middleware, routers
│   ├── core/                   config, security (JWT/RBAC), pipeline core,
│   │                           rate limit, errors, logging, middleware
│   ├── api/                    routes: screening, auth, audit, investigation,
│   │                           demo, health
│   ├── db/                     SQLAlchemy models, engine, mock-registry seed
│   ├── schemas/                request DTOs (pydantic)
│   ├── pipelines/              the six analytic stages + wiring
│   └── services/
│       ├── orchestrator.py     Step-9 coordination + audit event
│       ├── screening_service.py upload storage, run lifecycle, history
│       ├── ai_providers.py     the provider seam (Protocols + registry)
│       ├── crypto.py           Fernet at-rest encryption
│       ├── user_service.py     bootstrap admin, login, lockout, RBAC admin
│       ├── investigation_service.py  Step-12 search/stats/case inspector
│       ├── demo_mode.py        Step-14 scripted cases + honest checkpoints
│       ├── cleanup_service.py  upload retention cleanup
│       ├── registry_service.py MOCK registry lookups
│       ├── audit/              hashing, chain client, ledger backends
│       ├── ocr/                preprocessing, RapidOCR/Tesseract, MRZ (ICAO
│       │                       9303 check digits), field extraction
│       ├── validation/         rule engine + rulesets + MOCK registry
│       ├── tampering/          ELA, noise, compression, copy-move,
│       │                       metadata, regions, fusion (0-100 verdicts)
│       ├── face/               YuNet detection, SFace embeddings, quality
│       └── risk/               deterministic weighted-rules fusion
├── frontend/                   React + TypeScript + Vite dashboard
│   └── src/{api,components}/   typed client, panels, Investigation, Demo
├── blockchain/                 Hardhat + AuditAnchor.sol + deploy script
├── scripts/                    fixture/model/demo helpers (see §9)
├── tests/                      300 tests across 14 suites (Step-13 pyramid)
├── data/                       uploads/, mock_registry/, demo_fixtures/
├── models/                     YuNet + SFace ONNX weights (gitignored)
├── docker-compose.yml          caddy → api → hardhat (+ chain-deploy)
├── Dockerfile                  api image (models baked at build time)
└── .env.example                every documented env var
```

---

## 3. Setup instructions (local dev, Windows)

Requires **Python 3.12** and **Node 18+**.

```bash
# backend
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
copy .env.example .env          # then edit (see §4)

# face-verification models (one-time; ~37 MB, fully offline afterwards)
.venv/Scripts/python scripts/get_face_models.py

# frontend
cd frontend
npm install
```

## 4. Environment variables

Copy `.env.example` → `.env`. Everything has a safe dev default; prod
**refuses to boot** without the two secrets.

| Variable | Purpose | Dev default |
|---|---|---|
| `APP_ENV` | `dev` \| `prod`; prod enforces guardrails | `dev` |
| `APP_MOCK_MODE` | mock/demo data only, never real lookups | `true` |
| `APP_DB_URL` | SQLite dev default; Postgres-ready | `sqlite:///data/app.db` |
| `APP_SECRET_KEY` | JWT signing; **required ≥32 chars in prod** | dev fallback |
| `APP_ENCRYPTION_KEY` | Fernet key for `report_json`; **required in prod** | off (warned) |
| `APP_BOOTSTRAP_ADMIN_USERNAME/PASSWORD` | first ADMIN (env-only; 60 s token, forced rotation) | unset = skip |
| `APP_CORS_ORIGINS` | allowed browser origins | localhost:5173 |
| `APP_AI_*_PROVIDER` | provider selection per capability | real engines |
| `APP_OCR_*`, `APP_TAMPERING_*` | engine tuning + thresholds | see .env.example |
| `APP_RISK_BAND_*` | LOW/MEDIUM/HIGH ceilings (24/49/74) | spec defaults |
| `APP_CHAIN_RPC_URL` | local EVM RPC for anchors | `127.0.0.1:8545` |
| `APP_LEDGER_BACKEND` | `local` \| `evm` \| `auto` | `auto` |
| `APP_RATE_LIMIT_*` | per-client limits (auth/upload/analyze) | 10/30/20 per min |
| `APP_UPLOAD_RETENTION_HOURS` | upload dir deletion | 24 |
| `APP_MAX_UPLOAD_BYTES` | upload cap | 10 MB |

Generate secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 5. Database setup

None required for the demo — SQLite at `data/app.db` is auto-created on
startup, with tables, the clearly-marked MOCK registry
(`MOCK-*` document numbers) and the bootstrap admin seeded by the lifespan
hook. A lightweight dev auto-migration adds new nullable columns to older
dev DBs. For Postgres, set `APP_DB_URL=postgresql+psycopg://user:pass@host/db`.

## 6. AI model setup

| Capability | Engine | Model files |
|---|---|---|
| Preprocessing | OpenCV (deskew/perspective/denoise/CLAHE) | — |
| OCR | RapidOCR (ONNX, bundled) + ICAO 9303 MRZ parser | bundled |
| Validation | deterministic rule engine + MRZ cross-checks | — |
| Tampering | forensic heuristics (ELA, noise, grid, copy-move, metadata) | — |
| Face 1:1 | YuNet detector + SFace embeddings | `scripts/get_face_models.py` → `models/` |
| Presentation cues | Passive, image-only (screen-banding FFT + glare) — NOT liveness proof | — |
| Risk | explainable weighted rules (0-100 + contributions) | — |

Without the face weights the face stage honestly reports
`model_unavailable` → INCONCLUSIVE → human review. Everything else runs.

## 7. Blockchain setup

```bash
# Terminal 1 — local permissioned chain
cd blockchain && npx hardhat node

# Terminal 2 — deploy AuditAnchor (writes deployed.json + abi/)
cd blockchain && npm run deploy
```

With the node running and `APP_LEDGER_BACKEND=auto` (default), analyze
anchors the report hash on-chain via a background task and
`GET /api/audit/verify/{id}` returns **UNCHANGED** + `ledger_backend: evm`.
With the node offline, anchors honestly stay `pending`; the DB audit trail
(`audit_events.payload_sha256`) is always active.

## 8. Backend & frontend startup

```bash
# backend (terminal 3)
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
#   Swagger: http://localhost:8000/docs   Health: http://localhost:8000/health

# frontend (terminal 4)
cd frontend && npm run dev
#   http://localhost:5173  (Vite proxies /api to :8000 — zero CORS setup)
```

First login: the bootstrap credentials from `.env`
(`APP_BOOTSTRAP_ADMIN_*`). The bootstrap token expires in **60 seconds** —
change the password immediately (the UI banner links
`POST /api/auth/change-password`). Roles: SECURITY_OFFICER runs screenings,
REVIEWER inspects, ADMIN manages users.

### Docker (single-command demo)

```bash
# server-side .env next to docker-compose.yml (see DEPLOY.md §B)
docker compose up -d --build
docker compose logs chain-deploy        # Exited(0) = contract deployed
curl -sk https://demo.localhost/health  # {"status":"ok",...}
```

Only caddy is published (80/443). api + hardhat are internal; SQLite,
uploads and logs live in private volumes.

## 9. Testing instructions

```bash
.venv/Scripts/python -m pytest -q                # full suite (~5-6 min): 300 passed
.venv/Scripts/python -m pytest -m unit -q        # fast in-process tests
.venv/Scripts/python -m pytest -m security -q    # Step-11 controls
.venv/Scripts/python -m pytest -m e2e -q         # upload → analyze → audit → verify
.venv/Scripts/python -m pytest tests/test_step13_failures.py -q   # failure modes

cd frontend && npx tsc -b && npm run build       # typecheck + production build
```

Fixture/demo helpers: `scripts/make_demo_fixtures.py` (regenerate synthetic
SPECIMEN fixtures), `scripts/demo_run_cases.py` (CLI rehearsal),
`scripts/get_face_models.py` (model weights). See `TESTING.md` for the full
per-layer strategy.

## 10. SIH demo instructions (5-minute flow)

1. **Login** — show the role chip, the generic-error login (try a wrong
   password), and mention lockout + rate limiting.
2. **🎬 SIH Demo tab → Run case 1 (`case1_valid`)** — walk the green
   checkpoints: OCR fields + confidences, MRZ check digits ✓, validation
   PASS, tampering clean, face MATCH, LOW risk. Point at the honest
   "always review human" policy flag.
3. **Run case 2 (`case2_tampered`)** — tampering indicators + increased
   risk + human review. Open **Open full report** → Tampering panel
   (per-indicator severity/confidence).
4. **Run case 3 (`case3_identity_mismatch`)** — the impostor pattern:
   validation passes, face NO_MATCH, HIGH risk with the explainable
   "impostor" contribution row.
5. **Run case 4 (`case4_low_quality`)** — OCR fails honestly; review routing.
6. **Human Review tab** — the closing half of human-in-the-loop: the queue
   lists every case you just ran. Decide case 3 (**Escalate**, add a note)
   and case 1 (**Cleared**). Point out: the decision is attributed to your
   account, the case status flips to `reviewed`, and a `review_decided`
   audit event is written — the AI never closed a case; a human did.
7. **New Screening tab** — upload any fixture (or your own image) with an
   optional probe photo; watch all six stages complete live. (Gotcha ready:
   a multi-page PDF discloses "page 1 of N screened" in the report.)
8. **Blockchain panel** — "Log audit record" → anchor on-chain; badge
   **BLOCKCHAIN VERIFIED · UNCHANGED**; show the hash-only note (no PII
   on-chain).
9. **Investigation tab** — filters, charts, click a case → risk
   contributions + integrity verdict.
10. Close on the footer honesty line: *advisory AI, human decisions, mock
    registry, no government connection.*

**CLI rehearsal (no browser):**
```bash
.venv/Scripts/python scripts/demo_run_cases.py --with-review
```
`--with-review` records the plausible reviewer decision per case
(cleared/flagged/escalated/inconclusive) through the SAME review service
the UI uses, printing a `HUMAN:` line per run — a full pipeline + HITL
rehearsal in one command.

Every response is labeled DEMONSTRATION/SIMULATED in demo mode; checkpoints
that fail are shown as failed — nothing is papered over.

## 11. Troubleshooting guide

| Symptom | Cause → Fix |
|---|---|
| `401` on every API call | Token expired (30 min) → sign in again (the UI auto-signs out). |
| Login says invalid credentials but you're sure | Account locked after 5 failures (15 min) or bootstrap password never rotated. Reset via `APP_BOOTSTRAP_ADMIN_PASSWORD` on a fresh DB. |
| `429 Too many attempts` | Per-client rate limit (login 10/min). Wait a minute. Behind a proxy the key uses `X-Forwarded-For` correctly; direct clients can't spoof it. |
| Face stage = MODULE PENDING / INCONCLUSIVE "model unavailable" | Run `python scripts/get_face_models.py`; check `models/` has both ONNX files. |
| OCR weak on a real scan | Try higher resolution (≥1400 px wide), straighten, set the right doc-type hint; Tesseract fallback: install the binary + `APP_OCR_ENGINE=auto`. |
| Upload rejected (415) | Extension **and** magic bytes must match an allowed image/PDF; images must decode (50 MP budget). Re-export the file. |
| Anchors stay `pending` | Chain offline → start `npx hardhat node` + `npm run deploy`; check `APP_CHAIN_RPC_URL` and `blockchain/deployed.json`. |
| Verify says NOT ON LEDGER | The record was never logged for that backend → click "Log audit record" (or POST `/api/audit/log`). |
| `RuntimeError: APP_ENV=prod requires APP_SECRET_KEY/APP_ENCRYPTION_KEY` | Set both secrets in the environment (see §4). |
| `Demo fixtures are not generated yet` | Run `python scripts/make_demo_fixtures.py`. |
| Frontend can't reach the API | Dev: backend must run on :8000 (Vite proxies). Deployed: set `VITE_API_BASE` and rebuild. |
| Port already in use | Change `--port` (backend), `server.port` in `vite.config.ts` (frontend), or stop the other process. |
| Windows console Unicode errors in the demo CLI | `scripts/demo_run_cases.py` already prints ASCII marks — use it, not a custom script. |
| Stale DB after schema change | Delete `data/app.db` (dev only) and restart — it reseeds. |

---

## 12. Review findings (Step 15) — what was fixed

**Human-in-the-loop completed (Step 15 review):** the AI never decides, and now
a human formally does — `POST /api/review/{run_id}/decide` (REVIEWER/ADMIN)
persists cleared/flagged/escalated/inconclusive, flips the case to
`reviewed`, and writes a hash-only `review_decided` audit event. A Human
Review tab lists the pending queue. Previously the `reviews` table existed
but no decision could ever be recorded.

**PDFs are now analyzable:** uploads accepted `.pdf` but every CV stage
would have failed on the raw bytes. Page 1 is rasterized (PyMuPDF, 200 DPI,
pixel-budgeted); multi-page PDFs disclose `pdf_pages` vs
`pdf_pages_screened = 1` and take a risk point for incomplete coverage.

**Face anti-spoof honesty:** multiple faces in the PROBE frame (impostor
standing next to the holder) now force INCONCLUSIVE before any comparison,
and passive presentation cues (screen-recapture banding, glare) are
reported with the explicit note that they are NOT liveness guarantees.

**Rate-limit key hardened:** the per-client key previously took the
LEFTMOST `X-Forwarded-For` hop — client-controlled, so a header like
`X-Forwarded-For: 1.2.3.4` rotated buckets at will. It now takes the
rightmost hop (the one the trusted proxy appended), still only for
private-network peers.

**Bugs fixed**
1. `GET /api/auth/roles` crashed with `NameError` (`ROLES` not imported in
   `routes_auth.py`) — 500 on a documented endpoint.
2. `orchestrator._attach_audit` used `scalar_one_or_none()` over all audit
   events → `MultipleResultsFound` for any screening that also had a
   Step-10 `audit_logged` event. Now deterministically picks the freshest
   `screening_completed` event.
3. `chain_client.anchor` idempotent path wrote the block **timestamp** into
   `block_number` — misleading audit data. Now returns `block_timestamp`
   and leaves `block_number` null.
4. `providers_status()` had an operator-precedence bug
   (`and` before `or`) making `is_placeholder` wrong for every
   non-preprocessing capability.
5. `BlockchainPanel` performed a **write-on-read**: merely viewing the
   panel auto-POSTed `/api/audit/log` (surprising side effects + 403 noise
   for REVIEWERs). Logging is now the explicit button only.
6. Rate limiting keyed on the socket IP → behind Caddy all visitors shared
   one bucket (one burst locked the whole demo out). Now a proxy-aware key
   that trusts `X-Forwarded-For` only from private-network peers.
7. Dev JWT fallback secret was <32 bytes (PyJWT `InsecureKeyLengthWarning`
   on every token) — replaced with a 48-byte clearly-named fallback; prod
   still requires a real secret.
8. Document/probe file URLs ignored `VITE_API_BASE` → broken previews on
   split-origin deployments.

**UX/robustness**
9. Auto sign-out on any 401 mid-session (expired token previously left a
   stuck half-broken dashboard).
10. Login no longer masks 429/5xx/network errors as "Invalid username or
    password" (those are shown honestly; 401 keeps the generic message).
11. Uploads and demo runs now record `operator_id` (auth existed but the
    attribution was never wired through).

**Known limitations (documented, honest — by design for the MVP)**
- Risk bands/thresholds are policy, not fact; heuristics have false
  positives/negatives (each panel says so).
- JWT revocation denylist and rate-limit storage are in-process: fine for
  the single-node demo; Redis/DB-backed for multi-node.
- `human_review_required` is always true — the system deliberately never
  auto-accepts/rejects.
- There is NO liveness detection: only passive presentation cues. A printed
  photo or screen re-capture of the genuine holder can defeat the face
  module; the cue panel and the report say so explicitly. Interactive
  challenge–response liveness is the documented upgrade path.
- Face embeddings are SFace (128-d), not a top-N FRVT model; threshold
  0.363 is OpenCV's reference value, not a calibrated operating point on
  any real population.
- OCR covers TD3/MRV-B MRZ well; TD1/TD2 parse but are less exercised;
  visual-zone extraction is regex-level (labeled fields only).
- SQLite + per-stage CPU work (seconds per run) is appropriate for the
  demo scale; Postgres + a worker queue is the production path.
- The UI password-rotation dialog is a documented follow-up (endpoint
  exists; banner links it).
