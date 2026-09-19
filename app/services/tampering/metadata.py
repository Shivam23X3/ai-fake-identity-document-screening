"""Metadata forensics (detector 5): EXIF consistency without EXIF libraries.

Reads raw JPEG/PNG/TIFF segments at the byte level:

- JPEG APP segments: Exif (APP1), JFIF (APP0), XMP (APP1 alt), ICC (APP2)
- TIFF IFD0/ExifIFD walking for selected tags (Software, Make, Model, DateTime...)
- PNG text chunks (tEXt/iTXt/zTXt) — the usual home of editor fingerprints

No EXIF library is required; Pillow is already a dependency and its module
name (``PIL.TiffTags``) supplies tag-number → name mappings.
"""
from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path
from typing import Any

from app.services.tampering.constants import (
    CAMERA_SOFTWARE_TOKENS,
    EDITED_QT_STD_TABLES,
    METADATA_ANON_SOFTWARE_TOKENS,
    METADATA_SOFTWARE_KEYS,
)

logger = logging.getLogger(__name__)

# TIFF type codes → struct format + byte size
_TIFF_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _ascii(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


# ---------------------------------------------------------------------------
# JPEG segment scan
# ---------------------------------------------------------------------------
def _scan_jpeg(data: bytes) -> dict[str, Any]:
    info: dict[str, Any] = {
        "has_exif": False, "has_xmp": False, "has_jfif": False, "has_icc": False,
        "exif_tiff": None, "quant_tables": [], "segments": [],
    }
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return info

    off = 2
    n = len(data)
    while off + 4 <= n:
        if data[off] != 0xFF:
            break
        marker = data[off + 1]
        if marker in (0xD8, 0xD9):          # SOI / EOI
            off += 2
            continue
        if marker == 0xDA:                   # SOS: entropy-coded data follows
            break
        if 0xD0 <= marker <= 0xD7:           # RST
            off += 2
            continue
        seg_len = struct.unpack(">H", data[off + 2: off + 4])[0]
        payload = data[off + 4: off + 2 + seg_len]
        info["segments"].append(f"0x{marker:02X}")
        if marker == 0xE0 and payload[:4] == b"JFIF":
            info["has_jfif"] = True
        elif marker == 0xE1:
            if payload[:6] == b"Exif\x00\x00":
                info["has_exif"] = True
                tiff = payload[6:]
                if info["exif_tiff"] is None:
                    info["exif_tiff"] = _parse_tiff(tiff)
            elif b"http://ns.adobe.com/xap/" in payload[:60]:
                info["has_xmp"] = True
        elif marker == 0xE2 and payload[:11] == b"ICC_PROFILE":
            info["has_icc"] = True
        elif marker == 0xDB:                 # quantization tables
            p = 0
            while p + 1 < len(payload):
                pq = payload[p] >> 4
                tq = payload[p] & 0x0F
                step = 64 * (2 if pq else 1)
                p += 1 + step
                info["quant_tables"].append(tq)
        off += 2 + seg_len
    return info


# ---------------------------------------------------------------------------
# TIFF/EXIF IFD walking
# ---------------------------------------------------------------------------
def _parse_tiff(tiff: bytes) -> dict[str, Any] | None:
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return None
    out: dict[str, Any] = {"endian": tiff[:2].decode("ascii"), "tags": {}}
    try:
        _walk_ifd(tiff, endian, 8, out["tags"], depth=0)
    except Exception as exc:  # noqa: BLE001 - malformed EXIF must not kill analysis
        logger.debug("TIFF walk stopped early: %s", exc)
    return out


def _walk_ifd(tiff: bytes, endian: str, ifd_off: int, tags: dict, depth: int) -> None:
    if depth > 3 or ifd_off + 2 > len(tiff):
        return
    (count,) = struct.unpack_from(endian + "H", tiff, ifd_off)
    count = min(count, 512)  # sanity cap
    next_off = ifd_off + 2 + 12 * count
    for i in range(count):
        entry = ifd_off + 2 + 12 * i
        if entry + 12 > len(tiff):
            return
        tag, typ, num = struct.unpack_from(endian + "HHI", tiff, entry)
        size = _TIFF_SIZES.get(typ, 1) * num
        if size <= 4:
            raw = tiff[entry + 8: entry + 8 + size]
        else:
            (val_off,) = struct.unpack_from(endian + "I", tiff, entry + 8)
            raw = tiff[val_off: val_off + size] if val_off + size <= len(tiff) else b""
        if typ == 2:
            tags[tag] = _ascii(raw)
        elif typ in (3, 8):
            vals = struct.unpack_from(endian + ("H" * min(num, 8)), raw) if raw else ()
            tags[tag] = vals[0] if len(vals) == 1 else list(vals)
        elif typ in (4, 9):
            vals = struct.unpack_from(endian + ("I" * min(num, 8)), raw) if raw else ()
            tags[tag] = vals[0] if len(vals) == 1 else list(vals)
        elif typ == 5:                        # rationals (Exif uses these a lot)
            tags[tag] = f"rational:{num}"
        else:
            tags[tag] = f"<{typ}:{num}>"
    if next_off + 4 <= len(tiff):
        nxt = struct.unpack_from(endian + "I", tiff, next_off)[0]
        if nxt:
            _walk_ifd(tiff, endian, nxt, tags, depth + 1)


# Tag numbers we care about (PIL.TiffTags supplies names when importable).
_TAG_NAMES: dict[int, str] = {
    0x010F: "Make", 0x0110: "Model", 0x0131: "Software", 0x013B: "Artist",
    0x0132: "DateTime", 0x9003: "DateTimeOriginal", 0x9004: "DateTimeDigitized",
    0x9286: "UserComment", 0x9C9B: "XPTitle", 0x9C9C: "XPComment",
    0x9C9D: "XPAuthor", 0x9C9E: "XPKeywords", 0x0128: "ResolutionUnit",
    0x011A: "XResolution", 0x011B: "YResolution", 0x8769: "ExifIFDPointer",
    0x8825: "GPSIFDPointer", 0xA005: "InteropIFDPointer",
}


def _named_tags(tags: dict[int, Any]) -> dict[str, Any]:
    named: dict[str, Any] = {}
    for num, val in tags.items():
        name = _TAG_NAMES.get(num, f"tag_{num:04X}")
        named[name] = val
        if name.endswith("IFDPointer") and isinstance(val, int):
            pass  # pointer values themselves are not user-facing
    return named


# ---------------------------------------------------------------------------
# PNG chunk scan
# ---------------------------------------------------------------------------
def _scan_png(data: bytes) -> dict[str, Any]:
    info: dict[str, Any] = {"has_exif": False, "text_chunks": {}, "chunks": []}
    off, n = 8, len(data)
    while off + 8 <= n:
        try:
            (length,) = struct.unpack_from(">I", data, off)
            ctype = data[off + 4: off + 8]
            body = data[off + 8: off + 8 + length]
        except struct.error:
            break
        if length > n - off:
            break
        name = ctype.decode("ascii", "replace")
        info["chunks"].append(name)
        if ctype == b"tEXt" and b"\x00" in body:
            k, v = body.split(b"\x00", 1)
            info["text_chunks"][_ascii(k)] = _ascii(v)
        elif ctype == b"iTXt" and b"\x00" in body:
            k, rest = body.split(b"\x00", 1)
            parts = rest.split(b"\x00", 2)
            if len(parts) == 3:
                val = parts[2]
                if parts[0] == b"\x01":       # compressed flag
                    try:
                        val = zlib.decompress(parts[2] if len(parts) == 3 else b"")
                    except zlib.error:
                        val = b""
                info["text_chunks"][_ascii(k)] = val.decode("utf-8", "replace").strip()
        elif ctype == b"eXIf":
            info["has_exif"] = True
        off += 8 + length + 4               # length + type + body + CRC
    return info


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def read_metadata(path: str | Path) -> dict[str, Any]:
    """Byte-level metadata summary of an image file (never raises)."""
    path = Path(path)
    out: dict[str, Any] = {
        "format": path.suffix.lower().lstrip("."),
        "size_bytes": path.stat().st_size if path.is_file() else None,
        "has_exif": False, "has_xmp": False, "has_icc": False, "has_jfif": False,
        "tags": {}, "text_chunks": {}, "quant_tables": [], "parse_warning": None,
    }
    try:
        data = path.read_bytes()
    except OSError as exc:
        out["parse_warning"] = f"unreadable file: {exc}"
        return out

    try:
        if data[:2] == b"\xff\xd8":
            jpeg = _scan_jpeg(data)
            out.update(
                has_exif=jpeg["has_exif"], has_xmp=jpeg["has_xmp"],
                has_icc=jpeg["has_icc"], has_jfif=jpeg["has_jfif"],
                quant_tables=jpeg["quant_tables"],
            )
            tiff = jpeg.get("exif_tiff")
            if tiff:
                out["tags"] = _named_tags(tiff.get("tags", {}))
                out["exif_endianness"] = tiff.get("endian")
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            png = _scan_png(data)
            out["has_exif"] = png["has_exif"]
            out["text_chunks"] = png["text_chunks"]
    except Exception as exc:  # noqa: BLE001
        out["parse_warning"] = f"{type(exc).__name__}: {exc}"
    return out


def _token_in(value: str, tokens: tuple[str, ...]) -> bool:
    v = value.lower()
    return any(tok in v for tok in tokens)


def analyze_metadata(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn raw metadata into indicator dicts (may be empty = nothing odd)."""
    indicators: list[dict[str, Any]] = []
    tags = meta.get("tags") or {}

    def _add(severity: str, confidence: float, note: str, details: dict[str, Any]) -> None:
        indicators.append(
            {"type": "metadata_anomaly", "severity": severity,
             "confidence": round(confidence, 3), "note": note, "details": details}
        )

    # 1. Editor software fingerprints.
    software_values = [str(v) for k, v in tags.items() if k in METADATA_SOFTWARE_KEYS]
    for k, v in (meta.get("text_chunks") or {}).items():
        lv = str(v).lower()
        if any(tok in lv for tok in METADATA_ANON_SOFTWARE_TOKENS):
            software_values.append(f"{k}: {v}")
    editor_hits = [s for s in software_values if _token_in(s, METADATA_ANON_SOFTWARE_TOKENS)]
    if editor_hits:
        _add(
            "medium", 0.7,
            "Metadata references photo-editing software.",
            {"found": editor_hits[:3]},
        )
    elif meta.get("has_xmp"):
        # XMP without a recognized editor token: information only.
        _add("info", 0.2, "XMP metadata packet present (no editor identified).", {})

    # 2. JPEG that claims camera provenance but has no camera make/model.
    if meta.get("format") == "jpg" and not meta.get("has_exif"):
        _add(
            "low", 0.45,
            "JPEG file with all EXIF stripped — provenance unknown.",
            {"note": "common after re-saving from editors/chat apps"},
        )
    elif meta.get("has_exif") and tags:
        has_make = bool(tags.get("Make")) or bool(tags.get("Model"))
        software = str(tags.get("Software", ""))
        if not has_make and software and not _token_in(software, CAMERA_SOFTWARE_TOKENS):
            _add(
                "low", 0.4,
                "EXIF Software tag present but no camera Make/Model.",
                {"software": software[:60]},
            )

    # 3. Timestamp sanity: DateTimeOriginal missing while DateTime present.
    dt_original = tags.get("DateTimeOriginal")
    dt_file = tags.get("DateTime")
    if dt_file and not dt_original and meta.get("has_exif"):
        _add("info", 0.25, "Capture timestamp (DateTimeOriginal) absent; only file-level time present.", {})

    # 4. Quantization tables: re-saved JPEGs commonly use the editor's own
    #    table (standard table 0). Weak signal — many cameras do this too.
    qts = meta.get("quant_tables") or []
    if qts and meta.get("format") == "jpg":
        nonstd = [t for t in qts if t not in EDITED_QT_STD_TABLES]
        if nonstd:
            _add(
                "info", 0.3,
                "Non-standard JPEG quantization table id — typical of re-encoding.",
                {"tables": sorted(set(qts))},
            )
    return indicators
