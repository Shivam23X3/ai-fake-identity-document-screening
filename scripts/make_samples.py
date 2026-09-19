"""Generate synthetic test documents (passport + visa) as PNG images.

The images are machine-printed with real ICAO 9303 MRZ check digits so the
full OCR pipeline can be exercised end-to-end without any real documents.

    .venv/Scripts/python scripts/make_samples.py
    → data/samples/sample_passport.png, sample_visa.png
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ocr.mrz import mrz_check_digit  # noqa: E402


def _pad(text: str, width: int) -> str:
    return text + "<" * (width - len(text))


def passport_mrz() -> tuple[str, str]:
    line1 = _pad("P<UTOSMITH<<ALICE<JANE", 44)
    number = "L898902C3"
    dob, expiry = "740812", "280415"
    personal = _pad("Z184226B", 14)
    num_chk = str(mrz_check_digit(number))
    dob_chk = str(mrz_check_digit(dob))
    exp_chk = str(mrz_check_digit(expiry))
    per_chk = str(mrz_check_digit(personal))
    # TD3 line 2 (44): num(9)+chk nat(3) dob(6)+chk sex exp(6)+chk personal(14)+chk composite(1)
    composite_payload = number + num_chk + "UTO" + dob + dob_chk + "F" + expiry + exp_chk + personal + per_chk
    line2 = number + num_chk + "UTO" + dob + dob_chk + "F" + expiry + exp_chk + personal + per_chk + str(mrz_check_digit(composite_payload))
    return line1, line2


def visa_mrz() -> tuple[str, str]:
    line1 = _pad("V<UTOSEDERBERG<<KARL<THEO", 36)
    number = "AB9876543"
    dob, expiry, stay = "850615", "291231", "90"
    payload = (
        number + str(mrz_check_digit(number))
        + "UTO"
        + dob + str(mrz_check_digit(dob))
        + "M"
        + expiry + str(mrz_check_digit(expiry))
        + "M"
        + stay + str(mrz_check_digit(stay))
    )
    line2 = _pad(payload, 35) + str(mrz_check_digit(payload))
    return line1, line2


def _label(img, text, xy, scale=0.55, color=(40, 40, 40)):
    cv2.putText(img, text, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def _value(img, text, xy, scale=0.75):
    cv2.putText(img, text, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, (15, 15, 15), 2, cv2.LINE_AA)


def make_passport(path: Path) -> Path:
    img = np.full((720, 1024, 3), 245, dtype=np.uint8)
    img[:] = (232, 226, 210)  # beige-ish page
    cv2.rectangle(img, (20, 20), (1003, 699), (120, 110, 90), 2)
    _label(img, "REPUBLIC OF UTOPIA - PASSPORT", (60, 70), 0.8, (90, 70, 50))
    _label(img, "Type / Code / Country", (60, 120))
    _value(img, "P", (60, 150), 0.6)
    _value(img, "UTO", (140, 150), 0.6)
    _label(img, "Surname", (60, 200))
    _value(img, "SMITH", (200, 200))
    _label(img, "Given Names", (60, 250))
    _value(img, "ALICE JANE", (200, 250))
    _label(img, "Passport No.", (560, 200))
    _value(img, "L898902C3", (700, 200))
    _label(img, "Nationality", (560, 250))
    _value(img, "UTOPIAN", (700, 250))
    _label(img, "Date of Birth", (60, 310))
    _value(img, "12 AUG 1974", (200, 310))
    _label(img, "Sex", (560, 310))
    _value(img, "F", (700, 310))
    _label(img, "Date of Expiry", (60, 370))
    _value(img, "15 APR 2028", (200, 370))
    _label(img, "Personal No.", (560, 370))
    _value(img, "Z184226B", (700, 370))
    # Photo placeholder
    cv2.rectangle(img, (760, 420), (960, 640), (200, 200, 200), -1)
    cv2.circle(img, (860, 500), 40, (150, 150, 150), -1)
    cv2.ellipse(img, (860, 590), (55, 40), 0, 0, 180, (150, 150, 150), -1)
    # MRZ
    l1, l2 = passport_mrz()
    cv2.rectangle(img, (20, 640), (1003, 700), (250, 250, 250), -1)
    _value(img, l1, (35, 668), 0.62)
    _value(img, l2, (35, 694), 0.62)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return path


def make_visa(path: Path) -> Path:
    img = np.full((560, 900, 3), 248, dtype=np.uint8)
    cv2.rectangle(img, (15, 15), (884, 544), (100, 90, 160), 2)
    _label(img, "UTOSLAND VISA - VISA", (50, 60), 0.85, (60, 40, 130))
    _label(img, "Visa Type", (50, 110))
    _value(img, "C-1 WORK", (180, 110))
    _label(img, "Visa No.", (50, 170))
    _value(img, "AB9876543", (180, 170))
    _label(img, "Surname", (50, 230))
    _value(img, "SEDERBERG", (180, 230))
    _label(img, "Given Names", (50, 290))
    _value(img, "KARL THEO", (180, 290))
    _label(img, "Entries", (500, 170))
    _value(img, "M", (620, 170))
    _label(img, "Duration of Stay", (500, 230))
    _value(img, "90 DAYS", (620, 230))
    _label(img, "Date of Birth", (50, 350))
    _value(img, "15 JUN 1985", (180, 350))
    _label(img, "Date of Expiry", (500, 350))
    _value(img, "31 DEC 2029", (620, 350))
    l1, l2 = visa_mrz()
    cv2.rectangle(img, (15, 460), (884, 545), (250, 250, 250), -1)
    _value(img, l1, (28, 488), 0.6)
    _value(img, l2, (28, 514), 0.6)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return path


if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parents[1] / "data" / "samples"
    p = make_passport(out_dir / "sample_passport.png")
    v = make_visa(out_dir / "sample_visa.png")
    print(f"Wrote {p}\nWrote {v}")
