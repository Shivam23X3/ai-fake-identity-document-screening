# Blockchain audit layer (Step 9)

Tamper-evident anchoring of screening reports on a **local EVM chain**.
The chain stores ONLY hashes — the full report (and all PII) stays in the
local database. Anyone can re-hash the stored report and compare against
the on-chain commitment: silent modification of DB records becomes
detectable.

```
report (DB) ──sha256(canonical JSON)──▶ AuditAnchor.sol (local chain)
                                            ▲ re-hash + compare to verify
```

## Files

| Path | Purpose |
|---|---|
| `contracts/AuditAnchor.sol` | Append-only `(screeningId, reportHash)` commitments |
| `scripts/deploy.js` | Deploys + writes `deployed.json` and `abi/AuditAnchor.json` (read by the Python client) |
| `test/audit.test.js` | Contract tests (anchor, duplicate revert, independence, zero-lookup) |
| `hardhat.config.js` | Local network config (chainId 31337) |

## Setup

```bash
cd blockchain
npm install
npx hardhat compile
npx hardhat test          # contract tests
```

## Run

```bash
# terminal 1: local chain
npm run node

# terminal 2: deploy (writes deployed.json + abi/)
npm run deploy
```

The backend picks the deployment up automatically (`blockchain/deployed.json`).
Until both are running, audit anchors honestly stay `pending` — the DB audit
trail works regardless, and no transaction is ever faked.

## Environment (backend side, see .env.example)

```
APP_CHAIN_RPC_URL=http://127.0.0.1:8545
APP_CHAIN_CONTRACT_ADDRESS=      # empty → read from blockchain/deployed.json
APP_CHAIN_PRIVATE_KEY=           # empty → unlocked Hardhat account #0
```

⚠ `APP_CHAIN_PRIVATE_KEY` is for LOCAL TEST ACCOUNTS ONLY (the default
Hardhat accounts are publicly known). Never fund or reuse these keys
elsewhere.
