"""Blockchain-anchored audit trail — Step 9.

Two layers, both honest:

1. **DB audit trail** (always available): every screening produces an
   ``audit_events`` row whose ``payload_sha256`` commits to the exact
   canonical report JSON. Re-hashing the stored report detects silent
   modification of the DB row.

2. **On-chain anchoring** (optional): the same hash is stored on a local
   EVM chain (Hardhat node by default) through :class:`AuditAnchorClient`.
   When no chain is reachable the anchor stays ``pending`` — the audit
   trail still exists and the report hash is still committed; we never
   fake a transaction hash.

The chain stores ONLY hashes (plus the run id and a timestamp) — PII never
leaves the local database. See ARCHITECTURE.md §7.
"""
