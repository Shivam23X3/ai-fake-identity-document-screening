"""web3.py client for the AuditAnchor contract on a local EVM chain.

Design:
- **Lazy, never-crashing**: :meth:`AuditAnchorClient.available` probes the
  RPC endpoint; every failure degrades to ``available=False`` so the
  screening workflow continues without the chain (the DB audit trail still
  commits the report hash).
- **Hashes only**: the contract stores ``keccak256(reportHash_abi)`` — a
  32-byte commitment of the run id + report hash + timestamp. No PII ever
  leaves the process.
- **Idempotent**: the contract reverts with ``AlreadyAnchored`` when the
  same (runId, reportHash) pair is stored twice; the client treats that as
  success and returns the existing anchor.

Configuration (see .env.example): ``APP_CHAIN_RPC_URL``,
``APP_CHAIN_CONTRACT_ADDRESS`` (hex, or a path to
``blockchain/deployed.json``), ``APP_CHAIN_PRIVATE_KEY`` (local test
accounts only — the default is the well-known Hardhat account #0).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parents[3]
_DEPLOYED_JSON = _BASE / "blockchain" / "deployed.json"
_ABI_JSON = _BASE / "blockchain" / "abi" / "AuditAnchor.json"

# keccak256("AuditAnchor") used as the linkage proof on-chain.
# b"" = not computed yet (computed lazily so import never needs web3).
_LINKAGE_CODE_HASH: bytes = b""


def _load_abi() -> list[dict[str, Any]]:
    try:
        data = json.loads(_ABI_JSON.read_text(encoding="utf-8"))
        # Hardhat artifact format: { abi: [...] }
        if isinstance(data, dict) and "abi" in data:
            return data["abi"]
        return data  # bare ABI list
    except Exception:  # noqa: BLE001 - missing artifact must not crash import
        return []


def _resolve_contract_address() -> str | None:
    """Address from settings, or from blockchain/deployed.json when present."""
    settings = get_settings()
    addr = getattr(settings, "chain_contract_address", "") or ""
    if addr.startswith("0x") and len(addr) == 42:
        return addr
    try:
        if _DEPLOYED_JSON.is_file():
            data = json.loads(_DEPLOYED_JSON.read_text(encoding="utf-8"))
            candidate = str(data.get("address", ""))
            if candidate.startswith("0x") and len(candidate) == 42:
                return candidate
    except Exception:  # noqa: BLE001
        pass
    return None


def _linkage_code_hash() -> bytes:
    """keccak256('AuditAnchor') as 32 bytes — web3 v7 requires bytes32 args."""
    global _LINKAGE_CODE_HASH
    if not _LINKAGE_CODE_HASH:
        try:
            from web3 import Web3

            _LINKAGE_CODE_HASH = bytes(Web3.keccak(text="AuditAnchor"))
        except Exception:  # noqa: BLE001
            _LINKAGE_CODE_HASH = b""
    return _LINKAGE_CODE_HASH


class AnchorResult(dict):
    """Dict with convenience attrs (status, tx_hash, ...)."""


class AuditAnchorClient:
    """Thin, failure-tolerant web3.py wrapper around AuditAnchor.sol."""

    def __init__(self) -> None:
        self._w3: Any = None
        self._contract: Any = None
        self._probed: bool = False
        self._available: bool = False
        self._account: Any = None

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------
    def _connect(self) -> bool:
        if self._probed:
            return self._available
        self._probed = True
        settings = get_settings()
        abi = _load_abi()
        address = _resolve_contract_address()
        if not abi or not address:
            logger.info("Chain anchor unavailable: ABI or contract address missing")
            return False
        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(settings.chain_rpc_url, {"timeout": 10}))
            if not w3.is_connected():
                logger.info("Chain anchor unavailable: RPC %s not reachable", settings.chain_rpc_url)
                return False
            self._w3 = w3
            self._contract = w3.eth.contract(address=address, abi=abi)
            pk = settings.chain_private_key
            if pk:
                self._account = w3.eth.account.from_key(pk)
            else:
                # No signing key configured: use the node's unlocked coinbase
                # (works on default Hardhat/Ganache dev setups).
                try:
                    self._account = w3.eth.accounts[0]
                except Exception:  # noqa: BLE001
                    self._account = None
            self._available = True
            logger.info("Chain anchor connected: %s @ %s", address, settings.chain_rpc_url)
        except Exception as exc:  # noqa: BLE001 - never crash the pipeline
            logger.info("Chain anchor unavailable: %s", exc)
            self._available = False
        return self._available

    def available(self) -> bool:
        return self._connect()

    def status(self) -> dict[str, Any]:
        connected = self._connect()
        return {
            "implemented": connected,
            "engine": "web3-audit-anchor",
            "rpc_url": get_settings().chain_rpc_url,
            "contract": _resolve_contract_address(),
            "note": None if connected else "Local EVM node not reachable or contract not deployed; "
                                           "audit trail stays DB-only and anchors remain 'pending'.",
        }

    # ------------------------------------------------------------------
    # Anchoring
    # ------------------------------------------------------------------
    def anchor(
        self, run_id: str, report_hash: str, reviewer_decision_hash: str | None = None
    ) -> AnchorResult:
        """Store the report-hash commitment on-chain. Never raises."""
        t0 = time.perf_counter()
        if not self._connect():
            return AnchorResult(
                status="pending",
                tx_hash=None,
                block_number=None,
                on_chain_hash=None,
                error=None,
                note="Chain not reachable; hash committed to the DB audit trail only.",
            )
        try:
            return self._send_anchor(run_id, report_hash, reviewer_decision_hash, t0)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "AlreadyAnchored" in msg:
                # Honest idempotency: same run+hash already on chain.
                try:
                    on_chain = self._contract.functions.getAnchor(
                        _numeric_run_id(run_id), _to_bytes32(report_hash)
                    ).call()
                    return AnchorResult(
                        status="anchored",
                        tx_hash=None,
                        block_number=int(on_chain[2]),
            on_chain_hash=_to_bytes32(report_hash).hex(),
            duration_ms=int((time.perf_counter() - t0) * 1000),
            note="already anchored (idempotent)",
                    )
                except Exception:  # noqa: BLE001
                    pass
            logger.warning("Chain anchor failed for %s: %s", run_id, exc)
            return AnchorResult(
                status="failed",
                tx_hash=None,
                block_number=None,
                on_chain_hash=None,
                error=msg[:300],
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )

    def _send_anchor(
        self,
        run_id: str,
        report_hash: str,
        reviewer_decision_hash: str | None,
        t0: float,
    ) -> AnchorResult:
        w3 = self._w3
        contract = self._contract
        numeric_id = _numeric_run_id(run_id)  # contract key for this run
        report_b32 = _to_bytes32(report_hash)
        reviewer_b32 = _to_bytes32(reviewer_decision_hash or report_hash)
        linkage = _linkage_code_hash()

        # Read-modify-send: build the tx, sign if we have a key, else send raw
        # through the unlocked account (Hardhat auto-unlocks its accounts).
        fn = contract.functions.anchor(
            numeric_id, report_b32, reviewer_b32, linkage
        )
        if hasattr(self._account, "address") and self._account is not None and getattr(
            self._account, "key", None
        ) is not None:
            tx = fn.build_transaction(
                {
                    "from": self._account.address,
                    "nonce": w3.eth.get_transaction_count(self._account.address),
                    "gas": 500_000,
                    "chainId": w3.eth.chain_id,
                }
            )
            signed = w3.eth.account.sign_transaction(tx, self._account.key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        else:
            tx_hash = fn.transact({"from": self._account, "gas": 500_000})
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        ok = receipt.status == 1
        on_chain = contract.functions.getAnchor(numeric_id, report_b32).call()
        return AnchorResult(
            status="anchored" if ok else "failed",
            tx_hash=tx_hash.hex(),
            block_number=int(receipt.blockNumber),
            on_chain_hash=bytes(on_chain[0]).hex() if on_chain else None,
            linkage_code_hash=linkage.hex(),
            duration_ms=int((time.perf_counter() - t0) * 1000),
            error=None if ok else "transaction reverted",
        )

    # ------------------------------------------------------------------
    # Verification (audit the audit trail)
    # ------------------------------------------------------------------
    def verify(self, run_id: str, report_hash: str) -> dict[str, Any]:
        """Re-read the on-chain commitment for (runId, reportHash)."""
        if not self._connect():
            return {"verified": False, "reason": "chain_unavailable"}
        try:
            numeric_id = _numeric_run_id(run_id)
            b32 = _to_bytes32(report_hash)
            anchor = self._contract.functions.getAnchor(numeric_id, b32).call()
            stored_hash, _, ts, linkage = anchor
            match = bytes(stored_hash) == bytes(b32)
            linkage_ok = linkage == _linkage_code_hash()
            return {
                "verified": bool(match and linkage_ok and ts > 0),
                "on_chain_hash": bytes(stored_hash).hex(),
                "expected_hash": b32.hex(),
                "linkage_code_hash_match": linkage_ok,
                "block_timestamp": int(ts),
            }
        except Exception as exc:  # noqa: BLE001
            return {"verified": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}


def _numeric_run_id(run_id: str) -> int:
    """Derive the contract's uint256 key from a run id.

    Screening run ids are uuid4 hex, so the first 8 chars parse as hex.
    Demo run ids (``demo_case1_valid_ab12cd34``) do NOT, and int(…, 16)
    would raise ValueError — anchor every screening the same way by
    falling back to keccak256(run_id) when the prefix is not hex.
    """
    try:
        return int(run_id[:8], 16)
    except ValueError:
        from web3 import Web3

        return int.from_bytes(Web3.keccak(text=run_id)[:4], "big")


def _to_bytes32(hex_hash: str) -> Any:
    """Hex digest string → 32-byte value (left-padded) for the contract."""
    from web3 import Web3

    raw = hex_hash.removeprefix("0x")
    if len(raw) < 64:
        raw = raw.rjust(64, "0")
    return Web3.to_bytes(hexstr=raw)[:32]


__all__ = ["AnchorResult", "AuditAnchorClient"]
