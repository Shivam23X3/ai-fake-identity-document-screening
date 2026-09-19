# ARCHITECTURE — AI-Based Fake Identity & Document Screening System

SIH 2026 · Version 1.0 · Status: **DRAFT FOR TEAM APPROVAL**

> **Positioning statement.** This is an AI-*assisted* decision-support tool for
> authorized border/security personnel. It never issues legal decisions. Every
> AI output carries a confidence score, and anything uncertain or high-risk is
> routed to a human reviewer whose decision is itself audited.

---

## 1. Technology Stack (student-team realistic, fully offline)

| Layer | Technology | Why this and not something else |
|---|---|---|
| Frontend | **React 18 + Vite + TypeScript** | Fast dev server, huge talent pool in student teams, component model fits the "pipeline stage cards" UI. TypeScript catches contract mismatches with the API early. |
| UI styling | **Tailwind CSS** (or plain CSS) | Zero-designer teams get consistent, decent UI fast. |
| Backend API | **Python 3.12 + FastAPI + Uvicorn** | Same language as all AI/CV libraries (no cross-service glue), async I/O for uploads, automatic OpenAPI/Swagger docs (great for demo + frontend codegen). |
| OCR | **PaddleOCR (PP-OCRv4)** + custom MRZ parser | Free, runs offline, best-in-class open-source accuracy on MRZ zones; the MRZ parser is pure Python so it's testable without the model. |
| Imaging / CV | **OpenCV + Pillow + NumPy** | Standard, well-documented, covers deskew/denoise/ELA/copy-move. |
| Face | **InsightFace (buffalo_l)** via `insightface`/`onnxruntime` | Free, offline, embedding+cosine-similarity is easy to explain to judges; CPU-usable. Swappable behind a stage interface. |
| Tampering | **OpenCV heuristics first** (ELA, noise inconsistency, copy-move keypoints), optional CNN later | Heuristics are explainable and need no training data — honest about being probabilistic signals, not proof. |
| Risk scoring | **Deterministic weighted rules engine** (Python) | Explainable by construction (every score shows its contributing signals) — critical for a decision-support ethics story. ML fusion can come later behind the same interface. |
| Database | **SQLite (dev) → PostgreSQL (prod)** via **SQLAlchemy 2.0** | One ORM, two engines; students demo from a laptop with zero DB setup, scale path exists. |
| Migrations | **Alembic** | Versioned schema changes; avoids "delete the db and pray". |
| Blockchain audit | **Hardhat (local EVM node) + Solidity + web3.py** | Real append-only ledger semantics offline; anchoring **only the SHA-256 of reports** on-chain keeps PII off the chain (see §7). |
| Auth | **JWT (python-jose) + passlib(bcrypt)**, role-based | Simple, standard, enough for operator/supervisor/admin demo. |
| Background work | **FastAPI BackgroundTasks** (now) → Celery+Redis (scale path) | No extra infra for the demo; the audit anchor is queued, not blocking. |
| Testing | **pytest + httpx** (backend), Vitest (frontend) | Industry default; FastAPI's TestClient makes pipeline tests trivial. |
| Packaging/demo | **docker-compose (optional)** | One-command judge demo if time permits; everything already runs laptop-local without it. |

**Deliberately excluded:** cloud OCR/APIs (offline requirement + cost + data-sovereignty
story), Celery/Redis initially (ops burden > benefit at demo scale), any government
DB integration (out of scope; mock registry only, clearly marked).

---

## 2. Complete Folder Structure (target state)

```
fake-id-screening/
├── README.md
├── ARCHITECTURE.md                  ← this document
├── requirements.txt                 # runtime deps
├── requirements-dev.txt             # pytest, ruff, etc.
├── .env.example                     # all config, no secrets
│
├── app/                             # ===== BACKEND =====
│   ├── main.py                      # FastAPI factory, routers, middleware
│   ├── core/                        # framework-level, no business logic
│   │   ├── config.py                # settings (env-driven), MOCK_MODE policy
│   │   ├── pipeline.py              # StageResult / PipelineStage / Pipeline engine
│   │   ├── responses.py             # response envelope + disclaimers
│   │   ├── security.py              # JWT, password hashing, role guards
│   │   └── logging.py               # PII-safe logging setup
│   ├── api/                         # HTTP layer only (validate → call service)
│   │   ├── routes_health.py
│   │   ├── routes_auth.py
│   │   ├── routes_screening.py      # upload + trigger pipeline
│   │   ├── routes_reviews.py        # human review workflow
│   │   ├── routes_audit.py          # audit trail + chain status
│   │   └── routes_mock_registry.py  # clearly-marked MOCK lookups
│   ├── pipelines/
│   │   └── __init__.py              # stage assembly/order (the only wiring file)
│   ├── stages/                      # ===== AI/ML MODULES (one file per stage) =====
│   │   ├── preprocessing.py         # deskew, denoise, glare, crop, quality metrics
│   │   ├── ocr.py                   # PaddleOCR + MRZ parse + field confidence
│   │   ├── validation.py            # check digits, expiry, cross-field, mock lookup
│   │   ├── tampering.py             # ELA, noise, copy-move, metadata forensics
│   │   ├── face.py                  # detect, embed, portrait-vs-photo similarity
│   │   └── risk.py                  # weighted fusion → band + human-review routing
│   ├── services/                    # business services used by API + stages
│   │   ├── registry_service.py      # MOCK document registry lookups
│   │   ├── audit_service.py         # writes DB event + anchors hash on chain
│   │   ├── blockchain_client.py     # web3.py wrapper around the contract
│   │   └── storage_service.py       # upload paths, hashing, cleanup
│   ├── db/
│   │   ├── base.py                  # SQLAlchemy engine/session
│   │   ├── models.py                # ORM models (schema in §5)
│   │   └── seed_mock_registry.py    # loads FAKE demo data (marked MOCK)
│   ├── schemas/                     # Pydantic request/response DTOs
│   └── utils/
│       ├── image_utils.py
│       ├── mrz.py                   # ICAO 9303 check-digit math
│       └── hashing.py               # sha256 helpers for audit
│
├── models/                          # downloaded weights (gitignored)
│   └── README.md                    # how to fetch paddle/insightface weights
│
├── frontend/                        # ===== FRONTEND (Step 8) =====
│   └── src/
│       ├── api/                     # typed API client (generated from OpenAPI)
│       ├── pages/                   # Login, Upload, ScreeningDetail, ReviewQueue, Audit
│       ├── components/              # StageCard, ConfidenceBar, RiskBadge, MrzView…
│       └── App.tsx
│
├── blockchain/                      # ===== AUDIT LAYER (Step 9) =====
│   ├── contracts/AuditAnchor.sol    # store(hash, runId, timestamp) append-only
│   ├── scripts/deploy.js            # deploy to local hardhat node
│   ├── test/audit.test.js
│   └── hardhat.config.js
│
├── data/
│   ├── uploads/                     # operator uploads (gitignored, PII-sensitive)
│   ├── samples/                     # synthetic watermarked SAMPLE documents
│   └── mock_registry/               # ⚠️ FAKE DEMO DATA ONLY (see README inside)
│
├── tests/
│   ├── conftest.py
│   ├── test_step1_skeleton.py       # ✔ exists
│   ├── test_preprocessing.py        # per-stage test files (Step 2+)
│   └── …
│
└── docs/
    ├── ethics_and_limitations.md    # model cards, known failure modes, disclaimers
    └── demo_script.md               # SIH presentation runbook
```

**Separation of concerns (hard rules)**
- `api/` never imports AI libraries — it only orchestrates services/pipelines.
- `stages/` never touches HTTP or the DB directly — they read/write `PipelineContext` and return `StageResult`.
- `services/` is the only layer that talks to DB/chain; `core/` knows nothing about business logic.
- Frontend only knows the REST contract (OpenAPI-generated types).
- Config/env lives only in `core/config.py` + `.env`.

---

## 3. Architecture Diagram

```
                                ┌──────────────────────────────────────────┐
                                │                 OPERATOR                 │
                                │   (authorized human — final decision)    │
                                └────────────┬─────────────────────────────┘
                                             │ HTTPS (LAN)
                                ┌────────────▼─────────────┐
                                │   FRONTEND (React+Vite)  │
                                │  Upload · Stage view ·   │
                                │  Review queue · Audit    │
                                └────────────┬─────────────┘
                                             │ REST/JSON + multipart
┌────────────────────────────────────────────▼──────────────────────────────────────────────┐
│                             BACKEND API — FastAPI (app/api)                                │
│   auth · screening · reviews · audit · mock-registry (JWT + role middleware)               │
└───────────────┬──────────────────────────────────────────────────────────────┬─────────────┘
                │                                                              │
     ┌──────────▼───────────────────────────────┐               ┌──────────────▼───────────┐
     │      PIPELINE ORCHESTRATOR (core/)       │               │   BUSINESS SERVICES      │
     │  StageResult contract · failure isolation│               │  (app/services)          │
     │  auto-skip on missing input · timing     │               │  audit_service           │
     └──────────┬───────────────────────────────┘               │  registry_service (MOCK) │
                │ runs in order                                  │  storage_service         │
     ┌──────────▼──────────────────────────────────────────────┐ └──────┬──────────┬────────┘
     │                AI / ML STAGE MODULES (app/stages)        │        │          │
     │                                                          │        │          │
     │  1 Preprocessing ── OpenCV/Pillow      (deskew, denoise, │        │          │
     │        glare fix, MRZ crop hint, quality metrics)        │        │          │
     │  2 OCR ─────────── PaddleOCR + MRZ parser (fields +     │        │          │
     │        per-field confidence)                             │        │          │
     │  3 Validation ──── ICAO check digits, expiry, format,   │        │          │
     │        cross-field, MOCK registry lookup                 │        │          │
     │  4 Tampering ───── ELA, noise-inconsistency, copy-move, │        │          │
     │        metadata forensics  (probabilistic signals only)  │        │          │
     │  5 Face ────────── InsightFace embeddings, portrait-vs- │        │          │
     │        photo cosine similarity, liveness heuristics      │        │          │
     │  6 Risk ────────── weighted fusion → score, band,       │        │          │
     │        human_review_required routing                     │        │          │
     └──────────────────────────────────────────────────────────┘        │          │
                                                                          │          │
                                             ┌────────────────────────────▼───┐   ┌──▼──────────────────────┐
                                             │  DATABASE (SQLite dev/Postgres)│   │  BLOCKCHAIN (Hardhat)   │
                                             │  users · screenings · reviews  │   │  AuditAnchor.sol        │
                                             │  audit_events · MOCK registry  │   │  stores SHA-256(report) │
                                             │  (PII lives here, never chain) │   │  + runId + timestamp    │
                                             └────────────────────────────────┘   └─────────────────────────┘
```

---

## 4. Data Flow (end-to-end)

1. **Upload** — operator authenticates (JWT), submits document image/PDF via React → `POST /api/v1/screen`. Backend validates type/size, stores under `data/uploads/<run_id>/original.<ext>`, computes `sha256(file)` immediately (integrity anchor used later by the audit trail).
2. **Pipeline run** — orchestrator executes stages 1→6 in order. Each stage reads required keys from `PipelineContext`, writes JSON-serializable outputs + a `StageResult{status, confidence, data, human_review_required}`. Crash in any stage → that stage marked `error`, run continues (policy: `skip`). Missing inputs → stage marked `skipped`.
3. **Risk fusion** — the risk stage combines: OCR field confidences, validation failures, tampering signals, face similarity, liveness cues → `risk_score ∈ [0,1]`, band `low|medium|high`, and routing: `high ⇒ human_review_required = true` (also forced by any stage error / missing confidence).
4. **Screening result** — API persists a `screenings` row (status `pending_review`), returns the full per-stage report to the UI with disclaimers. **No automatic accept/reject ever.**
5. **Human review** — reviewer opens the case in the review queue, sees stage cards + evidence, records decision (`cleared | flagged | escalated`) + notes → `screenings.review_status/decision/reviewer_id` updated.
6. **Audit anchoring** — `audit_service` (BackgroundTask) writes an `audit_events` row containing `sha256(canonical_report_json)`, then calls the Hardhat-deployed `AuditAnchor` contract → stores `(runId, reportHash, reviewerDecisionHash, timestamp)`. Row updated with `tx_hash`, `block_number`. Full report stays in the DB; the chain holds only hashes (tamper-evidence, not storage).
7. **Verification** — anyone can re-hash the stored report and compare against the on-chain hash: silent modification of DB records becomes detectable.

---

## 5. Database Schema

```
users
  id            INTEGER PK
  username      TEXT UNIQUE NOT NULL
  password_hash TEXT NOT NULL                 -- bcrypt
  role          TEXT NOT NULL                 -- 'operator' | 'supervisor' | 'admin'
  is_active     BOOLEAN DEFAULT 1
  created_at    TIMESTAMP DEFAULT now

screenings                                       -- one row per screening run
  id            INTEGER PK
  run_id        TEXT UNIQUE NOT NULL            -- uuid4 hex, public identifier
  operator_id   INTEGER FK users
  original_path TEXT NOT NULL                   -- data/uploads/<run_id>/...
  file_sha256   TEXT NOT NULL
  doc_type_hint TEXT                            -- operator-declared: passport/visa/...
  doc_type_detected TEXT                          -- from OCR stage, may be null
  status        TEXT NOT NULL                    -- 'processing'|'pending_review'|'reviewed'|'error'
  risk_score    REAL                             -- 0..1
  risk_band     TEXT                             -- 'low'|'medium'|'high'
  human_review_required BOOLEAN NOT NULL DEFAULT 1
  report_json   TEXT NOT NULL                    -- full per-stage report (JSON)
  created_at    TIMESTAMP
  completed_at  TIMESTAMP

reviews                                          -- human-in-the-loop decisions
  id            INTEGER PK
  screening_id  INTEGER FK screenings UNIQUE
  reviewer_id   INTEGER FK users
  decision      TEXT NOT NULL                    -- 'cleared'|'flagged'|'escalated'
  notes         TEXT
  decided_at    TIMESTAMP

audit_events                                     -- local, chain-anchored trail
  id            INTEGER PK
  screening_id  INTEGER FK screenings
  event_type    TEXT NOT NULL                    -- 'screening_completed'|'review_recorded'
  payload_sha256 TEXT NOT NULL                   -- sha256 of canonical JSON payload
  chain         TEXT DEFAULT 'hardhat-local'
  tx_hash       TEXT
  block_number  INTEGER
  anchor_status TEXT DEFAULT 'pending'           -- 'pending'|'anchored'|'failed'
  created_at    TIMESTAMP

mock_registry_documents                          -- ⚠️ 100% FAKE DEMO DATA
  id            INTEGER PK
  is_mock       BOOLEAN NOT NULL DEFAULT 1       -- schema-enforced marker
  doc_type      TEXT NOT NULL                    -- passport/visa/national_id/license/permit
  doc_number    TEXT NOT NULL
  full_name     TEXT NOT NULL
  date_of_birth TEXT
  expiry_date   TEXT
  status        TEXT NOT NULL                    -- 'active'|'expired'|'reported_lost'
  UNIQUE (doc_type, doc_number)
```

Design notes: stage outputs live in `report_json` (JSON column) rather than 20
thin tables — simpler for a student team, and the report is exactly what gets
hashed on-chain. `mock_registry_documents.is_mock` makes the fake-data policy
auditable at the schema level, not just by convention.

---

## 6. AI Pipeline Architecture

**Stage contract (already implemented in `app/core/pipeline.py`):**

```
PipelineStage:
    name: str
    requires: tuple[str, ...]     # context keys needed
    provides: tuple[str, ...]     # context keys produced
    run(ctx) -> StageResult

StageResult:
    stage, status(ok|not_implemented|error|skipped),
    confidence(0..1 | None), data(dict), error, human_review_required, duration_ms
```

**Context keys flowing between stages:**

```
upload ──▶ preprocess ──▶ ocr ──▶ validation ─┐
   │             │                             ├──▶ risk ──▶ report
   │             └──────────▶ tampering ────────┤
   └─────────────────────────▶ face ────────────┘

preprocess.provides: preprocessed_image_path, quality_metrics, mrz_region_hint
ocr.provides:        ocr_fields{field,value,confidence}, mrz_raw, doc_type_detected
validation.provides: validation{checks[], failures[], registry_hit(MOCK)}
tampering.provides:  tampering{signals[], score, explanation}
face.provides:       face_verification{similarity, threshold, liveness_cues}
risk.provides:       risk{score, band, contributions[], human_review_required}
```

**Principles**
- **Confidence everywhere**: every AI-derived field carries 0..1 confidence; the risk engine weights by confidence — uncertain inputs raise, not lower, scrutiny.
- **Graceful degradation**: missing/unreadable MRZ doesn't kill the run; downstream stages skip with `human_review_required = true`.
- **Failure isolation**: a crashing stage (e.g., model file missing) is reported, never fatal; policy configurable (`skip`/`fail`).
- **Swappability**: OCR engine, face model, tampering technique each sit behind their stage class — replace implementation, keep contract, tests unchanged.
- **Explainability**: every stage emits human-readable `explanation` strings + raw evidence (ELA map path, MRZ text, embedding distance) shown in the UI stage cards.
- **Honesty guardrails**: no stage may output a boolean "is_fake". Only probabilistic signals + confidence. Model versions logged into every report for reproducibility.

---

## 7. Security Architecture

**Identity & access**
- JWT bearer auth; roles: `operator` (runs screenings), `supervisor` (reviews/decides), `admin` (users + audit). Review endpoints reject non-supervisors — the human-in-the-loop is enforced by code, not convention.

**Input handling**
- Upload allow-list (jpg/png/webp/bmp/tiff/pdf), size cap (10 MB default), content-sniffing (magic bytes, not just extension), uploads stored outside web root with randomized `run_id` paths; optional ClamAV hook point documented.
- Images are treated as untrusted input: all decoding wrapped, EXIF sanitized, decompression-bomb guard via size + dimension caps.

**Data protection (PII)**
- All PII stays local: SQLite/Postgres + `data/uploads/`. **Never on-chain** (chain stores SHA-256 hashes only). Never in logs (PII-safe logging filter in `core/logging.py`).
- Retention policy config (`APP_RETENTION_DAYS`) + cleanup job; production notes: full-disk encryption, DB-at-rest encryption via Postgres TDE or OS-level, TLS via reverse proxy.
- Mock registry rows carry `is_mock=1`; API responses from it always include `"mock": true` so demo data can never masquerade as real intel.

**Audit integrity**
- Append-only `audit_events` + on-chain hash anchoring ⇒ DB tampering is detectable. Every report embeds file sha256, model versions, timestamps, operator id.

**Application hardening**
- CORS allow-list (frontend origin only), rate limiting on auth + upload, security headers middleware, `.env` secrets (never committed), dependencies pinned, `--reload` only in dev.

**Threat model (top risks, honest scope)**
| Threat | Mitigation |
|---|---|
| Forged document defeats AI | System never auto-clears; low confidence ⇒ human review by design |
| Attacker tampers with DB to hide a case | Hash-anchored audit trail on local chain |
| Adversarial images (crafted to fool CV) | Multi-signal fusion (OCR+forensics+face) raises cost; documented limitation, humans decide |
| Insider misuse / snooping | Role-based access, audit of every view/decision, retention limits |
| Model/weights swapped maliciously | Model versions + hashes recorded in each report |

**Ethics/limitations doc** (`docs/ethics_and_limitations.md`) ships with the repo: known failure modes, demographic-bias caveat for face models, "decision-support only" statement, no-government-integration statement.

---

## 8. Development Order (10 steps, each: build → test → fix → approve)

| # | Deliverable | Key files | Tests |
|---|---|---|---|
| 1 | **Skeleton + architecture** ✔ (skeleton done; this doc = approval gate) | `app/core/*`, `app/pipelines/*`, stub stages | 8 passing |
| 2 | Preprocessing stage (OpenCV) | `app/stages/preprocessing.py` | deskew/quality on synthetic tilted images |
| 3 | OCR stage (PaddleOCR + MRZ parser) | `app/stages/ocr.py`, `app/utils/mrz.py` | check-digit math unit tests + synthetic MRZ image |
| 4 | Validation engine + MOCK registry | `app/stages/validation.py`, `app/services/registry_service.py`, `app/db/*` | check digits, expiry, registry-hit paths |
| 5 | Tampering detection | `app/stages/tampering.py` | ELA on pristine vs edited sample |
| 6 | Face verification | `app/stages/face.py` | same-person vs different-person embeddings |
| 7 | Risk engine | `app/stages/risk.py` | band thresholds, routing rules, confidence weighting |
| 8 | React dashboard | `frontend/` | upload → stage cards → review flow (Vitest + manual) |
| 9 | Blockchain audit | `blockchain/`, `app/services/audit_service.py` | contract tests, anchor + verify round-trip |
| 10 | Integration, docker-compose, demo script, docs | `docs/`, `docker-compose.yml` | full end-to-end test, demo dry-run |

Steps 2–7 each only edit **one stage file** + its tests — the orchestrator, API, and frontend contracts stay frozen (that's the payoff of Step 1's design).
