"""Permissioned-ledger abstraction (Step 10).

Purpose (per the Step-10 requirements):

    1. A screening result is produced by the pipeline (Step 9 orchestrator).
    2. A canonical audit record is built: screening_id, timestamp, result
       hash, risk score, validation / tampering / face-verification status,
       system/version identifier.
    3. The record is hashed (sha256 of its canonical JSON form).
    4. ONLY that hash + minimal metadata go on the ledger.
    5. Sensitive data — document images, biometric data, personal fields —
       NEVER leaves the local database. The ledger sees hashes + statuses.

Abstraction: :class:`LedgerBackend` is the three-method interface
(``append`` / ``get`` / ``verify``). Backends shipped today:

- :class:`LocalLedger` — in-process mock permissioned ledger for demo/dev.
  Honest limitation: entries live only for the process lifetime, so it is
  NOT tamper-evident across restarts (and says so in its status).
- :class:`EVMLedger` — the AuditAnchor commitment on the local EVM chain
  (Hardhat). Works unchanged against any EVM permissioned ledger.

Selecting a backend is configuration, not code (``APP_LEDGER_BACKEND``:
``local`` | ``evm`` | ``auto``). A Hyperledger Fabric adapter would
implement the same three methods — nothing else in the app changes.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Protocol, runtime_checkable

from app.core.config import get_settings
from app.services.audit.hashing import sha256_hex

logger = logging.getLogger(__name__)

# Fields that must NEVER appear in a ledger payload (defense in depth:
# build_audit_record already excludes them; this guards future edits).
_FORBIDDEN_KEYS = {
    "image", "images", "photo", "portrait", "file", "files",
    "embedding", "embeddings", "biometric", "template",
    "full_name", "surname", "given_names", "date_of_birth",
    "passport_number", "nationality", "gender", "address",
    "national_id_number", "document_number", "personal_number",
    "raw_text", "mrz_raw", "original_path", "probe_image_path",
}


def build_audit_record(report: dict[str, Any]) -> dict[str, Any]:
    """Build the canonical Step-10 audit record from a Step-9 report.

    Contains ONLY the spec'd fields — no PII, no images, no biometrics:

    - screening_id        (the run id)
    - timestamp           (UTC ISO-8601)
    - result_hash         (sha256 of the canonical screening report)
    - risk_score          (0-100 fused score, or None)
    - validation_status   (is_valid: true/false/unknown)
    - tampering_status    (verdict string, or 'unknown')
    - face_match_status   (MATCH/NO_MATCH/INCONCLUSIVE, or 'unknown')
    - system_id           (app name + version + orchestrator id)
    """
    ra = report.get("risk_assessment") or {}
    validation = report.get("validation") or {}
    tampering = report.get("tampering") or {}
    face = report.get("face_verification") or {}

    # result_hash: the Step-9 report WITHOUT any audit block (the stored
    # report is exactly this; the key is defensive for in-memory payloads).
    hashable = {k: v for k, v in report.items() if k != "audit"}

    return {
        "screening_id": report.get("screening_id"),
        "timestamp": _utc_now_iso(),
        "result_hash": sha256_hex(hashable),
        "risk_score": ra.get("risk_score"),
        "validation_status": _validation_status(validation),
        "tampering_status": tampering.get("verdict") or "unknown",
        "face_match_status": face.get("match_status") or "unknown",
        "system_id": (
            f"{get_settings().app_name} v{get_settings().app_version}"
            f" | orchestrator={report.get('orchestrator', 'unknown')}"
        ),
    }


def validate_no_pii(record: dict[str, Any]) -> None:
    """Raise if a record meant for the ledger carries forbidden keys."""
    found = _FORBIDDEN_KEYS & set(record)
    if found:
        raise ValueError(f"PII/image data must never go on the ledger: {sorted(found)}")


def _validation_status(validation: dict[str, Any]) -> str:
    """is_valid boolean → stable status string (unknown when absent)."""
    v = validation.get("is_valid")
    if v is True:
        return "valid"
    if v is False:
        return "invalid"
    return "unknown"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------
@runtime_checkable
class LedgerBackend(Protocol):
    """The three methods any permissioned ledger must provide.

    ``entry`` is the flat, hash-only metadata dict produced by
    :func:`build_audit_record` (+ backend bookkeeping keys). Implementations
    must never raise for business reasons — they report failure in the
    returned dict so the audit layer can degrade honestly.
    """

    name: str

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Store the record's hash + metadata; return ledger bookkeeping."""
        ...

    def get(self, screening_id: str) -> dict[str, Any] | None:
        """Return the stored entry for one screening, or None."""
        ...

    def verify(self, screening_id: str, record_hash: str) -> dict[str, Any]:
        """Re-check a recomputed hash against the stored commitment."""
        ...


class LocalLedger:
    """In-process mock permissioned ledger (demo/dev backend).

    Stores full audit records keyed by screening id and keeps a sha256
    hash chain (each entry commits to the previous hash) so accidental
    in-memory mutation is detectable. NOT durable across restarts —
    ``status()`` discloses this honestly.
    """

    name = "local-mock-ledger"

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}
        self._prev_hash: str = "0" * 64

    # -- LedgerBackend -----------------------------------------------------
    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        validate_no_pii(record)
        screening_id = str(record.get("screening_id") or "")
        if not screening_id:
            return {"ledger_status": "failed", "error": "screening_id missing"}
        entry = {
            **record,
            "ledger": self.name,
            "sequence": len(self._entries) + 1,
            "prev_entry_hash": self._prev_hash,
            "entry_hash": self._entry_hash(record, self._prev_hash),
            "ledger_status": "logged",
        }
        self._entries[screening_id] = entry
        self._prev_hash = entry["entry_hash"]
        return entry

    def get(self, screening_id: str) -> dict[str, Any] | None:
        return self._entries.get(screening_id)

    def verify(self, screening_id: str, record_hash: str) -> dict[str, Any]:
        entry = self._entries.get(screening_id)
        if entry is None:
            return {
                "verified": False,
                "on_ledger": False,
                "reason": "no_ledger_entry",
                "backend": self.name,
            }
        stored = str(entry.get("result_hash") or "")
        return {
            "verified": stored == record_hash,
            "on_ledger": True,
            "stored_hash": stored,
            "expected_hash": record_hash,
            "backend": self.name,
            "sequence": entry.get("sequence"),
            "reason": None if stored == record_hash else "hash_mismatch",
        }

    # -- Extras -------------------------------------------------------------
    def _entry_hash(self, record: dict[str, Any], prev: str) -> str:
        return sha256_hex({**record, "prev_entry_hash": prev})

    def status(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "backend": self.name,
            "engine": "local-mock-ledger",
            "entries": len(self._entries),
            "note": (
                "In-process MOCK permissioned ledger: NOT durable across "
                "restarts and NOT tamper-evident beyond the process lifetime. "
                "Set APP_LEDGER_BACKEND=evm for the hash-anchored chain."
            ),
        }


class EVMLedger:
    """AuditAnchor-backed ledger (local EVM chain today; any EVM tomorrow).

    Stores ``keccak`` commitments keyed by (numeric run-id prefix, report
    hash). Metadata beyond the hash lives in the local DB (Step 9 trail) —
    the chain sees the hash only.
    """

    name = "evm-audit-anchor"

    def __init__(self) -> None:
        from app.services.audit.service import get_chain_client

        self._client = get_chain_client()

    # -- LedgerBackend -------------------------------------------------------
    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        validate_no_pii(record)
        screening_id = str(record.get("screening_id") or "")
        record_hash = str(record.get("result_hash") or "")
        if not screening_id or not record_hash:
            return {"ledger_status": "failed", "error": "screening_id/result_hash missing"}
        result = self._client.anchor(screening_id, record_hash)
        status = result.get("status")
        mapped = {"anchored": "anchored", "pending": "pending", "failed": "failed"}.get(
            status, "failed"
        )
        return {
            "ledger": self.name,
            "ledger_status": mapped,
            "tx_hash": result.get("tx_hash"),
            "block_number": result.get("block_number"),
            "note": result.get("note"),
            "error": result.get("error"),
        }

    def get(self, screening_id: str) -> dict[str, Any] | None:
        """EVM stores only the hash commitment; metadata lives in the DB."""
        if not self._client.available():
            return None
        return {
            "screening_id": screening_id,
            "ledger": self.name,
            "note": "Hash commitment lives on-chain; metadata in the local audit trail (DB).",
        }

    def verify(self, screening_id: str, record_hash: str) -> dict[str, Any]:
        result = self._client.verify(screening_id, record_hash)
        # A missing anchor reads back as a zeroed commitment (timestamp 0),
        # not an error — report it honestly as "not on ledger" so the caller
        # distinguishes "never logged" from "record changed".
        if int(result.get("block_timestamp") or 0) == 0:
            return {
                "verified": False,
                "on_ledger": False,
                "reason": "no_ledger_entry",
                "backend": self.name,
                "expected_hash": record_hash,
            }
        return {
            "verified": bool(result.get("verified")),
            "on_ledger": result.get("reason") != "chain_unavailable",
            "backend": self.name,
            **result,
        }

    def status(self) -> dict[str, Any]:
        base = self._client.status()
        return {
            "implemented": bool(base.get("implemented")),
            "backend": self.name,
            "engine": "web3-audit-anchor",
            "rpc_url": base.get("rpc_url"),
            "contract": base.get("contract"),
            "note": base.get("note"),
        }


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
_local_ledger: LocalLedger | None = None


def get_local_ledger() -> LocalLedger:
    """Process-wide local mock ledger (so demo entries survive across calls)."""
    global _local_ledger
    if _local_ledger is None:
        _local_ledger = LocalLedger()
    return _local_ledger


def get_ledger_backend() -> tuple[str, Any]:
    """Resolve the configured backend: ('local'|'evm', backend-instance)."""
    choice = (get_settings().ledger_backend or "auto").strip().lower()
    if choice == "local":
        return "local", get_local_ledger()
    if choice == "evm":
        return "evm", EVMLedger()
    # auto: prefer the real chain, fall back to the disclosed mock.
    evm = EVMLedger()
    if evm._client.available():
        return "evm", evm
    logger.info("Ledger auto: EVM chain unreachable; using local mock ledger")
    return "local", get_local_ledger()


def ledger_status() -> dict[str, Any]:
    """Diagnostics for /api/audit + the dashboard footer."""
    backend_name, backend = get_ledger_backend()
    info = backend.status() if hasattr(backend, "status") else {"implemented": False}
    return {"selected": backend_name, **info}


__all__ = [
    "EVMLedger",
    "LedgerBackend",
    "LocalLedger",
    "build_audit_record",
    "get_ledger_backend",
    "get_local_ledger",
    "ledger_status",
    "validate_no_pii",
]
