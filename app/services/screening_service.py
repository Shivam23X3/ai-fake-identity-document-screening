"""Screening service: upload storage, run lifecycle, history.

The API layer calls this service; it is the only place (besides the
pipeline itself) that knows how a screening run progresses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.models import Screening

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".pdf"}

# Magic-byte signatures for upload sniffing (content, not just extension).
_MAGIC: list[tuple[bytes, int, str]] = [
    (b"\xff\xd8\xff", 0, "jpeg"),
    (b"\x89PNG\r\n\x1a\n", 0, "png"),
    (b"BM", 0, "bmp"),
    (b"II*\x00", 0, "tiff"),
    (b"MM\x00*", 0, "tiff"),
    (b"%PDF", 0, "pdf"),
    (b"RIFF", 8, "webp"),  # RIFF....WEBP
]


def sniff_file_type(head: bytes) -> str | None:
    """Return detected type from magic bytes, or None if unknown."""
    for magic, offset, label in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            if label == "webp":
                if head[8:12] == b"WEBP":
                    return label
                continue
            return label
    return None


def new_run_id() -> str:
    return uuid.uuid4().hex


async def save_upload(filename: str, chunk_iter) -> dict:
    """Stream an upload to data/uploads/<run_id>/original.<ext>.

    Validates extension AND magic bytes, enforces the size cap, and
    computes the sha256 of the stored file. ``chunk_iter`` must be an
    async iterable of bytes (see routes_screening._upload_chunks).
    """
    settings = get_settings()
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AppError(
            f"Unsupported file type '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
            status_code=415,
        )

    run_id = new_run_id()
    upload_dir = settings.upload_dir / run_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"original{ext}"

    size = 0
    sha256 = hashlib.sha256()
    head = b""

    class _UploadRejected(Exception):
        pass

    try:
        with saved_path.open("wb") as out:
            async for chunk in chunk_iter:
                if not chunk:
                    continue
                if not head:
                    head = chunk[:16]
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise _UploadRejected(
                        f"File exceeds max size {settings.max_upload_bytes} bytes"
                    )
                sha256.update(chunk)
                out.write(chunk)
    except _UploadRejected as exc:
        _rmtree(upload_dir)
        raise AppError(str(exc), status_code=413) from exc
    except Exception:
        _rmtree(upload_dir)
        raise

    if size == 0:
        _rmtree(upload_dir)
        raise AppError("Empty upload", status_code=400)

    detected = sniff_file_type(head)
    if detected is None:
        _rmtree(upload_dir)
        raise AppError(
            "File content does not match an allowed image/PDF type (magic-byte check failed)",
            status_code=415,
        )

    # --- Malware-safe re-encode (Step 11) --------------------------------
    # Images (not PDFs) are re-encoded through Pillow: decoding a crafted
    # file fails hard on malformed payloads (embedded code, polyglots), and
    # the re-encode strips EXIF/app markers and any non-pixel payloads.
    # Decompression bombs are blocked by an explicit pixel budget BEFORE
    # decode (Pillow's own DecompressionBombError is a second gate).
    if detected != "pdf":
        reencoded = _reencode_image(saved_path, detected)
        if reencoded is not None:
            new_size = reencoded.stat().st_size
            sha256 = hashlib.sha256(reencoded.read_bytes())
            return {
                "run_id": run_id,
                "path": reencoded,
                "size": new_size,
                "sha256": sha256.hexdigest(),
                "detected_type": detected,
                "extension": reencoded.suffix.lower(),
            }
        _rmtree(upload_dir)
        raise AppError(
            "Image could not be decoded safely — rejected (malformed or oversized).",
            status_code=415,
        )

    # --- PDF handling ------------------------------------------------------
    # The CV pipeline works on images, so a PDF is rasterized (page 1) and
    # the pipeline analyzes the raster. The original PDF stays on disk as
    # the audit artifact; ``pdf_pages`` lets the report disclose that only
    # page 1 was screened.
    pdf_pages: int | None = None
    analyze_path = saved_path
    if detected == "pdf":
        from app.services.pdf_service import rasterize_pdf

        raster = rasterize_pdf(saved_path)
        if raster is not None:
            analyze_path = raster["raster_path"]
            pdf_pages = raster["pages"]
            sha256 = hashlib.sha256(analyze_path.read_bytes())
        # raster None ⇒ keep the PDF as analyze_path: every downstream stage
        # will report honestly that it could not process it.

    return {
        "run_id": run_id,
        "path": analyze_path,
        "size": size,
        "sha256": sha256.hexdigest(),
        "detected_type": detected,
        "extension": ext,
        "pdf_pages": pdf_pages,
    }


# Hard pixel budget: ~50 MP. A passport photo is ~2-12 MP; legitimate scans
# never approach this, while a 10000x10000 'image' would allocate ~300 MB
# per channel during decode (decompression-bomb DoS).
_MAX_PIXELS = 50_000_000


def _reencode_image(path: Path, detected: str) -> Path | None:
    """Decode + re-encode an image; None when the file cannot be decoded
    safely. Output keeps the original format (PNG→PNG, JPEG→JPEG, …),
    dropping all metadata (EXIF, XMP, embedded thumbnails, ancillary chunks)."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            if im.width * im.height > _MAX_PIXELS:
                return None
            im.load()  # force full decode now (catches truncated payloads)
            fmt = "PNG" if detected == "png" else "JPEG"
            out = path.with_suffix(".png" if fmt == "PNG" else ".jpg")
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGBA" if fmt == "PNG" else "RGB")
            elif im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out, format=fmt)
            return out
    except Exception:  # noqa: BLE001 - any decode anomaly ⇒ reject
        return None


def create_screening_row(
    session: Session,
    *,
    run_id: str,
    original_path: str,
    file_sha256: str,
    doc_type_hint: str,
    operator_id: int | None,
    probe_image_path: str | None = None,
    pdf_pages: int | None = None,
) -> Screening:
    from app.services.crypto import encrypt_text, encryption_enabled

    initial = json.dumps({"status": "uploaded"})
    row = Screening(
        run_id=run_id,
        operator_id=operator_id,
        original_path=original_path,
        probe_image_path=probe_image_path,
        file_sha256=file_sha256,
        doc_type_hint=doc_type_hint,
        status="processing",
        human_review_required=True,
        report_json=encrypt_text(initial) if encryption_enabled() else initial,
        report_encrypted=encryption_enabled(),
    )
    row.pdf_pages = pdf_pages
    session.add(row)
    session.flush()
    return row


def load_report(screening: Screening) -> dict:
    """Decrypt + parse the stored report (handles legacy plaintext rows)."""
    import json as _json

    from app.services.crypto import decrypt_text

    raw = screening.report_json
    if raw and screening.report_encrypted:
        raw = decrypt_text(raw)
    try:
        return _json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def get_by_run_id(session: Session, run_id: str) -> Screening | None:
    return session.execute(
        select(Screening).where(Screening.run_id == run_id)
    ).scalar_one_or_none()


def require_screening(session: Session, run_id: str) -> Screening:
    row = get_by_run_id(session, run_id)
    if row is None:
        raise AppError(f"Screening run '{run_id}' not found", status_code=404)
    return row


def list_history(session: Session, limit: int, offset: int) -> list[Screening]:
    stmt = (
        select(Screening)
        .order_by(Screening.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(stmt).scalars())


def save_report(
    session: Session,
    screening: Screening,
    *,
    report: dict,
    doc_type_detected: str | None,
    risk_score: float | None,
    risk_band: str | None,
    human_review_required: bool,
) -> None:
    from datetime import datetime, timezone

    from app.services.crypto import encrypt_text, encryption_enabled

    screening.report_json = (
        encrypt_text(json.dumps(report, default=str))
        if encryption_enabled()
        else json.dumps(report, default=str)
    )
    screening.report_encrypted = encryption_enabled()
    screening.doc_type_detected = doc_type_detected
    screening.risk_score = risk_score
    screening.risk_band = risk_band
    screening.human_review_required = human_review_required
    screening.status = "pending_review"
    screening.completed_at = datetime.now(timezone.utc)
    session.flush()


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
