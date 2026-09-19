"""Encryption at rest for sensitive columns (Step 11).

Decision: ``screenings.report_json`` holds OCR fields, MRZ lines, names and
dates — the most PII-dense artifact in the system. Database-level at-rest
encryption (SQLCipher / disk encryption) is the deployer's baseline; this
module adds APPLICATION-LEVEL encryption of that column so a stolen DB file
alone does not expose document contents (defense in depth, explicit key).

- Fernet (AES-128-CBC + HMAC-SHA256, authenticated): tampering the
  ciphertext is detected rather than silently decoded.
- Key from ``APP_ENCRYPTION_KEY`` (urlsafe base64, generate with
  ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``).
- Dev without a key: transparent plaintext passthrough with the loud
  startup warning (prototype stays runnable); prod REQUIRES the key
  (config guardrail) so silence is never an option there.
- A DB column flag records whether a row's payload is encrypted, keeping
  reads correct across the migration window (old plaintext rows remain
  readable until rewritten).
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None
_key_checked = False


def _get_fernet() -> Fernet | None:
    global _fernet, _key_checked
    if not _key_checked:
        _key_checked = True
        key = get_settings().encryption_key
        if key:
            try:
                _fernet = Fernet(key.encode())
            except Exception as exc:  # noqa: BLE001 - bad key config must fail loudly
                raise RuntimeError(f"APP_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc
    return _fernet


def encryption_enabled() -> bool:
    return _get_fernet() is not None


def encrypt_text(plaintext: str) -> str:
    """Encrypt a string; passthrough when no key is configured (dev only)."""
    f = _get_fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def decrypt_text(value: str) -> str:
    """Decrypt a stored value; plaintext passthrough (dev / legacy rows).

    A corrupted or wrong-key ciphertext raises InvalidToken — surfaced as a
    clear runtime error, NEVER silently decoded (that would defeat the
    integrity guarantee).
    """
    f = _get_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except InvalidToken:
        raise RuntimeError(
            "Stored data cannot be decrypted with APP_ENCRYPTION_KEY "
            "(wrong key or corrupted row). Refusing to return tampered data."
        )


__all__ = ["decrypt_text", "encrypt_text", "encryption_enabled"]
