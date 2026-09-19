"""Automatic cleanup of temporary uploaded images (Step 11).

Decision: uploads are DATA MINIMIZATION — the pipeline needs the original
image only until analysis completes, yet keeping it lets an operator
re-check the AI's reading against the document (a core honest-by-design
feature). Balance: retain for ``APP_UPLOAD_RETENTION_HOURS`` (default 24h)
then delete the whole per-run directory (original + preprocessed + probe).

Runs on startup and is exposed as a function for a future scheduler/cron.
Never deletes the mock registry or logs — only ``data/uploads/<run_id>/``
directories older than the cutoff.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def cleanup_expired_uploads(now: float | None = None) -> int:
    """Delete expired upload directories; returns how many were removed."""
    settings = get_settings()
    cutoff = (now if now is not None else time.time()) - settings.upload_retention_hours * 3600
    removed = 0
    try:
        root = settings.upload_dir
    except Exception:  # noqa: BLE001 - cleanup must never break startup
        return 0
    if not root.is_dir():
        return 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            # Directory mtime updates when files are added; a run untouched
            # for the whole retention window has no remaining operational use.
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                logger.info("Cleanup: removed expired upload dir %s", entry.name)
        except OSError as exc:
            logger.warning("Cleanup skipped %s: %s", entry.name, exc)
    return removed
