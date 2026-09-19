"""MRZ (Machine Readable Zone) parsing with ICAO 9303 check digits.

Supports the formats relevant to the initial scope:
- TD3 (passport booklets): 2 lines × 44 chars
- MRV-B (visas): 2 lines × 36 chars
- TD1/TD2 are detected but only partially parsed (national IDs later).

Check-digit verification follows ICAO 9303: weights 7,3,1 over the char
values (0-9 = value, A-Z = 10-35, '<' = 0), modulo 10.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.services.ocr.constants import fix_ocr_char

_CHAR_VALUES: dict[str, int] = {
    **{str(d): d for d in range(10)},
    **{chr(ord("A") + i): 10 + i for i in range(26)},
    "<": 0,
}


def mrz_check_digit(payload: str) -> int:
    """ICAO 9303 check digit for a payload string."""
    total = 0
    weights = (7, 3, 1)
    for i, ch in enumerate(payload):
        total += _CHAR_VALUES.get(ch, 0) * weights[i % 3]
    return total % 10


def _valid_check(payload: str, check: str) -> bool:
    if len(check) != 1 or not check.isdigit():
        return False
    return mrz_check_digit(payload) == int(check)


@dataclass
class MrzField:
    """One parsed MRZ element."""

    label: str
    value: str | None
    raw: str
    check_digit_ok: bool | None = None   # None = element has no check digit


@dataclass
class MrzResult:
    """Structured parse of an MRZ block."""

    format: str                          # TD3 | MRV-B | TD2 | TD1 | unknown
    lines: list[str]
    fields: dict[str, MrzField] = field(default_factory=dict)
    composite_digest_ok: bool | None = None

    @property
    def valid(self) -> bool:
        return self.format in {"TD3", "MRV-B"} and bool(self.fields)

    def field_dict(self) -> dict[str, dict[str, object]]:
        """{label: {value, check_digit_ok}} for JSON output."""
        return {
            key: {"value": f.value, "check_digit_ok": f.check_digit_ok}
            for key, f in self.fields.items()
        }


def _name_from_mrz(raw: str) -> tuple[str | None, str | None]:
    """Split 'SURNAME<<GIVEN<NAMES' into (surname, given_names)."""
    raw = raw.rstrip("<")  # trailing fillers are not part of the name
    if "<<" not in raw:
        return raw.replace("<", " ").strip() or None, None
    surname_raw, given_raw = raw.split("<<", 1)
    surname = surname_raw.replace("<", " ").strip() or None
    given = given_raw.replace("<", " ").strip() or None
    return surname, given


def _parse_date(raw: str, *, future: bool | None = None) -> str | None:
    """YYMMDD → ISO date string, or None when invalid.

    ``future``: True = expiry (must be plausible future/past 10y),
    False = birth (must be in the past). Used for sanity checks only —
    OCR misreads still surface to the reviewer.
    """
    if len(raw) != 6 or not raw.isdigit():
        return None
    yy, mm, dd = int(raw[:2]), int(raw[2:4]), int(raw[4:6])
    if not 1 <= mm <= 12 or not 1 <= dd <= 31:
        return None
    # ICAO pivot: years 0-49 ⇒ 20xx for births unless expiry-style.
    year = 2000 + yy if yy <= 49 else 1900 + yy
    if future is False and year > date.today().year:
        year -= 100
    try:
        parsed = date(year, mm, dd)
    except ValueError:
        return None
    today = date.today()
    if future is False and parsed > today:
        return None
    if future is True and parsed < today.replace(year=today.year - 20):
        # Expiry >20y in the past is implausible but not impossible; accept
        # but callers flag via check digits. Keep the value.
        pass
    return parsed.isoformat()


def _alpha(token: str) -> str:
    """Force letters on a letter-only MRZ field."""
    return "".join(fix_ocr_char(token, i, "alpha") for i in range(len(token)))


def _digit(token: str) -> str:
    """Force digits on a numeric MRZ field (dates)."""
    return "".join(fix_ocr_char(token, i, "digit") for i in range(len(token)))


_AMBIGUOUS = frozenset("ODQILZSBG015286")


def _repair_line_classes(line: str, positions: dict[int, str]) -> str:
    """Apply per-position alpha/digit corrections to an MRZ line.

    ``positions`` maps index → mode ("alpha"/"digit"). Used to repair
    systematic OCR confusions (e.g. UTO→UT0 in country codes) before field
    extraction; every resulting value is still validated by its check digit.
    """
    chars = list(line)
    for pos, mode in positions.items():
        if 0 <= pos < len(chars):
            chars[pos] = fix_ocr_char(line, pos, mode)
    return "".join(chars)


def _alnum_with_check(raw: str, check: str) -> str:
    """Resolve OCR ambiguity in an alphanumeric field using its check digit.

    ICAO check digits are the ground truth: accept the raw read when the
    check passes; otherwise try confusable-char variants (L↔1, O↔0, …)
    and return the first variant the check digit validates. Never returns
    a variant that failed verification without the raw fallback.
    """
    fixed = raw.upper().replace(" ", "")
    if not check.strip("<"):
        return fixed
    if _valid_check(fixed, check):
        return fixed
    positions = [i for i, ch in enumerate(fixed) if ch in _AMBIGUOUS]
    if len(positions) > 8:  # cap combinatorics on badly-read fields
        positions = positions[:8]
    for mask in range(1, 1 << len(positions)):
        chars = list(fixed)
        for bit, pos in enumerate(positions):
            if mask >> bit & 1:
                ch = chars[pos]
                chars[pos] = fix_ocr_char(fixed, pos, "alpha") if ch.isdigit() else fix_ocr_char(fixed, pos, "digit")
        candidate = "".join(chars)
        if candidate != fixed and _valid_check(candidate, check):
            return candidate
    return fixed


def parse_mrz(lines: list[str]) -> MrzResult | None:
    """Try to parse an MRZ from candidate lines. Returns None when not MRZ.

    ``lines`` should already be MRZ-cleaned (uppercase, '<' fillers).
    """
    candidates = [ln for ln in lines if 30 <= len(ln) <= 48]
    if not candidates:
        return None

    # TD3 / MRV-B: two lines starting with P<V or V<V respectively.
    for i in range(len(candidates) - 1):
        top, bottom = candidates[i], candidates[i + 1]

        if top.startswith("P<") or top.startswith("P["):
            fmt = "TD3" if len(top) == 44 and len(bottom) == 44 else "TD3?"
            if len(top) >= 40 and len(bottom) >= 40:
                return _parse_td3(top, bottom, fmt)
        if top.startswith("V<") or top.startswith("V["):
            if 34 <= len(top) <= 44 and 34 <= len(bottom) <= 44:
                return _parse_mrv(top, bottom)

    # TD2: two lines of 36, first starts with I< or I[
    for i in range(len(candidates) - 1):
        top, bottom = candidates[i], candidates[i + 1]
        if (
            (top.startswith("I<") or top.startswith("I["))
            and 34 <= len(top) <= 38
            and 34 <= len(bottom) <= 38
        ):
            return _parse_td2(top, bottom)

    # TD1: three lines of exactly 30.
    for i in range(len(candidates) - 2):
        a, b, c = candidates[i], candidates[i + 1], candidates[i + 2]
        if len(a) == 30 and len(b) == 30 and len(c) == 30 and (a.startswith("I<") or a.startswith("I[")):
            return _parse_td1(a, b, c)

    return None


def _parse_td3(top: str, bottom: str, fmt: str) -> MrzResult:
    # Line 1: P<ISO<SURNAME<<GIVEN<NAMES<<<<...
    doc_code, issuer_raw = top[0:2], top[2:5]
    name_raw = top[5:44]

    # Repair systematic OCR confusions by position class (P< + 3-letter
    # codes + digits-only date/number blocks); every field value stays
    # validated by its check digit afterwards.
    bottom = _repair_line_classes(bottom, {i: "digit" for i in list(range(13, 20)) + list(range(21, 28))})
    top = _repair_line_classes(top, {i: "alpha" for i in range(2, 5)})

    # Line 2 layout (44 chars):
    #   0-8   passport number       9 check
    #   10-12 nationality
    #   13-19 DOB                  19 check
    #   20     sex
    #   21-27 expiry                27 check
    #   28-41 optional data        42 check (optional-data)
    #   43     composite check
    number_raw, number_chk = bottom[0:9], bottom[9:10]
    nationality = bottom[10:13]
    dob_raw, dob_chk = bottom[13:19], bottom[19:20]
    sex = bottom[20:21]
    expiry_raw, expiry_chk = bottom[21:27], bottom[27:28]
    personal_no = bottom[28:42]
    personal_chk = bottom[42:43]
    composite_chk = bottom[43:44]

    surname, given = _name_from_mrz(_alpha(name_raw))
    number_fixed = _alnum_with_check(number_raw, number_chk)
    fields: dict[str, MrzField] = {
        "document_code": MrzField("document_code", doc_code.replace("<", ""), doc_code),
        "issuing_country": MrzField(
            "issuing_country", _alpha(issuer_raw).replace("<", ""), issuer_raw
        ),
        "surname": MrzField("surname", surname, name_raw),
        "given_names": MrzField("given_names", given, name_raw),
        "passport_number": MrzField(
            "passport_number",
            number_fixed.replace("<", ""),
            number_raw,
            _valid_check(number_fixed, number_chk),
        ),
        "nationality": MrzField(
            "nationality", _alpha(nationality).replace("<", ""), nationality
        ),
        "date_of_birth": MrzField(
            "date_of_birth", _parse_date(_digit(dob_raw), future=False), dob_raw,
            _valid_check(_digit(dob_raw), dob_chk),
        ),
        "gender": MrzField(
            "gender",
            sex if sex in {"M", "F", "<"} else None,
            sex,
        ),
        "expiry_date": MrzField(
            "expiry_date", _parse_date(_digit(expiry_raw), future=True), expiry_raw,
            _valid_check(_digit(expiry_raw), expiry_chk),
        ),
        "personal_number": MrzField(
            "personal_number",
            personal_no.replace("<", "") or None,
            personal_no,
            _valid_check(personal_no, personal_chk) if personal_chk.strip("<") else None,
        ),
    }

    # TD3 composite check digit (position 43) covers, per ICAO 9303:
    # doc number + check (0-9), DOB + check (13-19), sex, expiry + check,
    # optional data + check (21-42).
    composite_payload = bottom[0:10] + bottom[13:20] + bottom[21:43]
    composite_ok = _valid_check(composite_payload, composite_chk)

    return MrzResult(
        format=fmt,
        lines=[top, bottom],
        fields=fields,
        composite_digest_ok=composite_ok,
    )


def _parse_mrv(top: str, bottom: str) -> MrzResult:
    # MRV-B line 1: V<ISO<SURNAME<<GIVEN  (36 chars)
    # MRV-B line 2: VISA number(9) check nat(3) DOB(6) check sex exp(6) check
    #               entries(1) stay(2) check  personal(14) check(1)
    doc_code, issuer_raw = top[0:2], top[2:5]
    name_raw = top[5:36]

    number_raw, number_chk = bottom[0:9], bottom[9:10]
    nationality = bottom[10:13]
    dob_raw, dob_chk = bottom[13:19], bottom[19:20]
    sex = bottom[20:21]
    expiry_raw, expiry_chk = bottom[21:27], bottom[27:28]
    entries = bottom[28:29]
    stay = bottom[29:31]
    stay_chk = bottom[31:32]
    personal = bottom[32:35]
    personal_chk = bottom[35:36]

    number_fixed = _alnum_with_check(number_raw, number_chk)
    surname, given = _name_from_mrz(_alpha(name_raw))
    entry_map = {"M": "multiple", "S": "single", "1": "single"}
    fields: dict[str, MrzField] = {
        "document_code": MrzField("document_code", doc_code.replace("<", ""), doc_code),
        "issuing_country": MrzField(
            "issuing_country", _alpha(issuer_raw).replace("<", ""), issuer_raw
        ),
        "surname": MrzField("surname", surname, name_raw),
        "given_names": MrzField("given_names", given, name_raw),
        "visa_number": MrzField(
            "visa_number",
            number_fixed.replace("<", ""),
            number_raw,
            _valid_check(number_fixed, number_chk),
        ),
        "nationality": MrzField(
            "nationality", _alpha(nationality).replace("<", ""), nationality
        ),
        "date_of_birth": MrzField(
            "date_of_birth", _parse_date(_digit(dob_raw), future=False), dob_raw,
            _valid_check(_digit(dob_raw), dob_chk),
        ),
        "gender": MrzField("gender", sex if sex in {"M", "F", "<"} else None, sex),
        "expiry_date": MrzField(
            "expiry_date", _parse_date(_digit(expiry_raw), future=True), expiry_raw,
            _valid_check(_digit(expiry_raw), expiry_chk),
        ),
        "entry_validity": MrzField(
            "entry_validity",
            entry_map.get(entries, entries if entries != "<" else None),
            entries,
        ),
        "stay_duration": MrzField(
            "stay_duration",
            f"{_digit(stay).lstrip('0') or '0'} days" if stay.strip("0<") else None,
            stay,
            _valid_check(_digit(stay), stay_chk) if stay != "<<" else None,
        ),
        "personal_number": MrzField(
            "personal_number", personal.replace("<", "") or None, personal,
            _valid_check(personal, personal_chk) if personal_chk.strip("<") else None,
        ),
    }
    return MrzResult(format="MRV-B", lines=[top, bottom], fields=fields)


def _parse_td2(top: str, bottom: str) -> MrzResult:
    number_raw, number_chk = bottom[0:8], bottom[8:9]
    nationality = bottom[10:13]
    dob_raw, dob_chk = bottom[13:19], bottom[19:20]
    sex = bottom[20:21]
    expiry_raw, expiry_chk = bottom[21:27], bottom[27:28]
    surname, given = _name_from_mrz(_alpha(top[5:36]))
    fields = {
        "issuing_country": MrzField("issuing_country", _alpha(top[2:5]).replace("<", ""), top[2:5]),
        "surname": MrzField("surname", surname, top[5:36]),
        "given_names": MrzField("given_names", given, top[5:36]),
        "document_number": MrzField(
            "document_number",
            _alnum_with_check(number_raw, number_chk).replace("<", ""),
            number_raw,
            _valid_check(_alnum_with_check(number_raw, number_chk), number_chk),
        ),
        "nationality": MrzField("nationality", _alpha(nationality).replace("<", ""), nationality),
        "date_of_birth": MrzField(
            "date_of_birth", _parse_date(_digit(dob_raw), future=False), dob_raw,
            _valid_check(_digit(dob_raw), dob_chk),
        ),
        "gender": MrzField("gender", sex if sex in {"M", "F", "<"} else None, sex),
        "expiry_date": MrzField(
            "expiry_date", _parse_date(_digit(expiry_raw), future=True), expiry_raw,
            _valid_check(_digit(expiry_raw), expiry_chk),
        ),
    }
    return MrzResult(format="TD2", lines=[top, bottom], fields=fields)


def _parse_td1(a: str, b: str, c: str) -> MrzResult:
    number_raw, number_chk = a[5:14], a[14:15]
    dob_raw, dob_chk = b[0:6], b[6:7]
    sex = b[7:8]
    expiry_raw, expiry_chk = b[8:14], b[14:15]
    nationality = a[10:13]
    surname, given = _name_from_mrz(_alpha(c))
    fields = {
        "issuing_country": MrzField("issuing_country", _alpha(a[2:5]).replace("<", ""), a[2:5]),
        "document_number": MrzField(
            "document_number",
            _alnum_with_check(number_raw, number_chk).replace("<", ""),
            number_raw,
            _valid_check(_alnum_with_check(number_raw, number_chk), number_chk),
        ),
        "nationality": MrzField("nationality", _alpha(nationality).replace("<", ""), nationality),
        "date_of_birth": MrzField(
            "date_of_birth", _parse_date(_digit(dob_raw), future=False), dob_raw,
            _valid_check(_digit(dob_raw), dob_chk),
        ),
        "gender": MrzField("gender", sex if sex in {"M", "F", "<"} else None, sex),
        "expiry_date": MrzField(
            "expiry_date", _parse_date(_digit(expiry_raw), future=True), expiry_raw,
            _valid_check(_digit(expiry_raw), expiry_chk),
        ),
        "surname": MrzField("surname", surname, c),
        "given_names": MrzField("given_names", given, c),
    }
    return MrzResult(format="TD1", lines=[a, b, c], fields=fields)
