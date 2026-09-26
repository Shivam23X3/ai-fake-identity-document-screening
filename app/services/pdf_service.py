"""PDF support: rasterize the first page of an uploaded PDF for the pipeline.

Uploads accept ``.pdf`` (magic-byte checked) but the CV pipeline (OCR,
tampering forensics, face detection) operates on images. Without
rasterization a PDF upload would reach the pipeline and fail honestly in
every stage — a legitimate input degraded by our own plumbing.

Rasterization contract:
- The FIRST page is rendered at ``target_dpi`` (default 200 — enough for OCR
  of machine-printed text; capped so a hostile page cannot consume CPU).
- Output is a JPEG (quality 92) stored next to the original as
  ``original.raster.jpg`` — the pipeline treats it exactly like any upload.
- Page count > 1 is recorded (``pdf_pages``) so the report can disclose that
  only page 1 was screened — multi-page documents route to human review.
- Any rasterization failure is honest: the caller degrades to the existing
  "not analyzable" path instead of fabricating a result.

PyMuPDF (pymupdf) is the renderer: self-contained wheels, no Ghostscript
binary, no Poppler system dependency.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Render resolution bounds: 72 DPI (screen) is too soft for OCR; 300 DPI is
# beyond what the OCR target width uses anyway. 200 DPI ≈ 1654 px wide for
# an A4 page — inside the pipeline's OCR sweet spot (≥1400 px).
DEFAULT_TARGET_DPI = 200
MIN_DPI = 96
MAX_DPI = 300
# A single rendered page must stay below this pixel budget (DoS guard for
# pathological page sizes, e.g. 200-inch-wide PDFs).
MAX_RASTER_PIXELS = 50_000_000  # same budget as the image decode gate


def rasterize_pdf(
    pdf_path: str | Path,
    *,
    target_dpi: int = DEFAULT_TARGET_DPI,
    out_dir: Path | None = None,
) -> dict | None:
    """Render page 1 of ``pdf_path`` to a JPEG; return info or None on failure.

    Returns ``{"raster_path", "dpi", "pages", "width_px", "height_px"}``.
    ``pages`` is the document's total page count (disclosure for the report).
    """
    path = Path(pdf_path)
    if not path.is_file():
        return None
    dpi = max(MIN_DPI, min(MAX_DPI, int(target_dpi)))
    out_dir = out_dir or path.parent
    out_path = out_dir / "original.raster.jpg"
    try:
        import pymupdf  # PyMuPDF; imported lazily so PDF-less deploys still boot

        with pymupdf.open(str(path)) as doc:
            pages = int(doc.page_count)
            if pages < 1:
                return None
            page = doc.load_page(0)
            zoom = dpi / 72.0
            mat = pymupdf.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            if pix.width * pix.height > MAX_RASTER_PIXELS:
                # Re-render capped: keep aspect, clamp the total pixel count.
                shrink = (MAX_RASTER_PIXELS / float(pix.width * pix.height)) ** 0.5
                pix = page.get_pixmap(
                    matrix=pymupdf.Matrix(zoom * shrink, zoom * shrink), alpha=False
                )
            pix.save(str(out_path), jpg_quality=92)
            return {
                "raster_path": out_path,
                "dpi": dpi,
                "pages": pages,
                "width_px": int(pix.width),
                "height_px": int(pix.height),
            }
    except Exception as exc:  # noqa: BLE001 - honest failure, never a crash
        logger.warning("PDF rasterization failed for %s: %s", path, exc)
        return None
    finally:
        # PyMuPDF keeps no handle after close, but the pix buffer is large;
        # drop the reference explicitly so peak memory stays bounded.
        pix = None  # noqa: F841 - intentional reference drop


__all__ = ["DEFAULT_TARGET_DPI", "MAX_RASTER_PIXELS", "rasterize_pdf"]
