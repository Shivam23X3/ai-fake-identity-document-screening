# Deployment Guide (SIH Demo)

Target: a single small VPS (2 vCPU / 4 GB RAM) running **Docker + Docker Compose**.
One public service (Caddy), everything else internal. All data is synthetic/mock —
this deployment never touches a real government registry and holds no real PII.

## A. Architecture

```
                 Internet (HTTPS 443 / HTTP 80)
                            │
                     ┌──────▼──────┐
                     │   caddy     │  static frontend (frontend/dist) + TLS
                     │  (public)   │  proxies /api, /health, /pipeline
                     └──────┬──────┘
                            │ internal docker network
                     ┌──────▼──────┐          ┌───────────────┐
                     │    api      │──────────│    hardhat    │
                     │  FastAPI    │  hashes  │  EVM node     │
                     │ port 8000   │  only    │  port 8545    │
                     └──────┬──────┘ (internal)└───────────────┘
                            │
        ┌───────────────────┼────────────────────────────┐
        │ volume api_data   │ volume api_logs            │ volume chain_artifacts
        │ SQLite + uploads  │ app logs                   │ deployed.json + ABI
        └───────────────────┴────────────────────────────┘
```

- **caddy** is the only service with published ports (80/443). It serves the
  built SPA and reverse-proxies the API same-origin (no CORS needed).
- **api** (FastAPI, `uvicorn app.main:app`) is internal-only; ONNX face models
  are baked into the image at build time.
- **hardhat** is internal-only (no published ports). `chain-deploy` is a
  one-shot service that compiles + deploys `AuditAnchor` and exports
  `deployed.json` + `abi/AuditAnchor.json` to the shared volume the API reads.
- Nothing real: `APP_MOCK_MODE=true`, registry rows are `MOCK-*` synthetic
  fixtures, demo cases run on synthetic SPECIMEN images.

## B. Required server-side `.env` (next to `docker-compose.yml`)

Create it on the server — **never commit it**:

```bash
cat > .env <<'EOF'
DOMAIN=demo.example.com            # DNS name → Let's Encrypt; raw IP → HTTP
APP_SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
APP_ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
APP_BOOTSTRAP_ADMIN_PASSWORD=<strong unique password>
APP_CORS_ORIGINS=https://demo.example.com
EOF
chmod 600 .env
```

Notes:
- `APP_ENV=prod` is set by compose; the app then **refuses to boot** without
  `APP_SECRET_KEY` (≥32 chars) and `APP_ENCRYPTION_KEY` (valid Fernet key).
- `APP_BOOTSTRAP_ADMIN_PASSWORD` seeds the first admin once; rotate it after
  first login (bootstrap tokens expire in 60 s by design).
- No other variables are required; safe defaults are baked into compose.

## C. Frontend deployment

Built inside `Dockerfile.frontend`: `npm ci && npm run build` with
`VITE_API_BASE=""` (same-origin). No manual build or upload needed.
The canonical `Caddyfile` at the repo root is mounted read-only into caddy.

## D. Backend deployment

Built from the root `Dockerfile` (python:3.12-slim + OpenCV deps + web3).
`scripts/get_face_models.py` runs **during the build** and bakes the ONNX
models into the image. SQLite + uploads live in the private `api_data`
volume; logs in `api_logs`.

## E. Blockchain deployment

`blockchain/Dockerfile` builds a Hardhat image used twice:
1. `hardhat` service — long-running local EVM node (`hardhat node`), internal only.
2. `chain-deploy` service — one-shot: `hardhat compile && hardhat run
   scripts/deploy.js --network localhost`, then copies `deployed.json` +
   `abi/AuditAnchor.json` into the `chain_artifacts` volume mounted at
   `/srv/app/blockchain` in the api container (exactly the paths
   `app/services/audit/chain_client.py` resolves). These artifacts contain
   hashes + a public contract address — no secrets.

`hardhat.config.js` accepts `CHAIN_RPC_URL` (set to `http://hardhat:8545`
inside compose); local dev behavior is unchanged.

## F. VPS commands (the only commands you need)

```bash
# 1. Install Docker (once): https://docs.docker.com/engine/install/
# 2. Get the code
git clone <your-repo-url> && cd <repo>
# 3. Create the server-side .env (see section B) — never committed
# 4. Build + start everything
docker compose up -d --build
# 5. Watch the chain-deploy one-shot finish, then verify
docker compose logs chain-deploy
docker compose ps
curl -sk https://DOMAIN/health        # → {"status":"ok",...}
# 6. Log in with the bootstrap admin (rotate the password immediately)
# 7. Update later
git pull && docker compose up -d --build
# 8. Teardown (keeps volumes; add -v to also wipe DB/uploads/certs)
docker compose down
```

## G. Final testing procedure

1. `docker compose ps` — caddy Up(healthy), api Up(healthy), hardhat Up, chain-deploy Exited(0).
2. Browser: open `https://DOMAIN` → login page loads over valid TLS.
3. Run the 4 scripted demo cases (Dashboard → Demo) — every checkpoint green.
4. Upload flow with a synthetic fixture — result + audit event appear.
5. Audit verify — `conclusion: UNCHANGED`, `ledger_backend: evm`.
6. `curl -sk https://DOMAIN/data/app.db` and `/logs/app.log` → 404 (SPA fallback, not served).
7. `docker compose exec api ls /srv/app/data` → present inside the container only; not routable from outside.

## H. Security posture (demo)

- Only 80/443 exposed; api (8000) and hardhat (8545) are internal-only.
- Uploads, SQLite DB, logs and models are private volumes; never public.
- Secrets enter via environment only — never baked into images, never committed.
- Report JSON is encrypted at rest (Fernet); uploads auto-expire
  (`APP_UPLOAD_RETENTION_HOURS`); JWT auth + rate limits stay active.
- Demo/mock honesty banners are preserved end-to-end.
