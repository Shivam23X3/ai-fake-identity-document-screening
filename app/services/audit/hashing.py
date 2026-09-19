"""Canonical hashing helpers for the audit trail.

The audit trail commits to the *exact bytes* of a canonical JSON dump:
``json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str,
ensure_ascii=False)`` encoded UTF-8. Verification re-dumps the stored
report the same way and compares hashes, so silent DB edits are detectable.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    """Deterministic JSON string (stable across runs and machines)."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )


def sha256_hex(payload: Any) -> str:
    """SHA-256 hex digest of the canonical JSON of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = ["canonical_json", "sha256_hex"]
