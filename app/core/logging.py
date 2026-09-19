"""PII-safe logging setup.

- Console handler + rotating file handler (logs/app.log).
- A redaction filter masks runs of 6+ digits in log lines (document
  numbers, MRZ digits, phone-like numbers) as defense-in-depth.
  Code must still never log raw PII in the first place.
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_DIGITS = re.compile(r"\d{6,}")


class PIIRedactionFilter(logging.Filter):
    """Masks long digit runs in log records (heuristic PII scrubbing)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        redacted = _DIGITS.sub("######", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure the root logger. Call once at app startup."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Replace any pre-existing handlers (uvicorn/preload duplicates).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(PIIRedactionFilter())
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(PIIRedactionFilter())
        root.addHandler(file_handler)

    # Access logger: request ids are NOT PII and must stay searchable, so it
    # bypasses the PII filter (own handler, no propagation to root).
    access = logging.getLogger("access")
    access.setLevel(root.level)
    access.propagate = False
    access.addHandler(console)
    if log_dir is not None:
        access.addHandler(file_handler)
