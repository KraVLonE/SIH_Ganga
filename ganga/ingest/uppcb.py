"""Parse UP Pollution Control Board monthly river reports.

Each report lists ~90 stations with DO (mg/L), BOD (mg/L), total coliform and fecal coliform
(MPN/100 mL) for one month. Older files are text PDFs in a legacy Hindi font; newer ones are
scans. Station codes and numbers are read; station names come from the CPCB registry.
"""

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import cv2
import pdfplumber

from ganga.ingest.rowtable import Row, Token, extract_rows
from ganga.ingest.tableocr import deskew, find_grid, render

VARIABLES = ("do", "bod", "tc", "fc")
LIMITS = {"do": (0.0, 25.0), "bod": (0.0, 400.0), "tc": (1.0, 1e9), "fc": (1.0, 1e9)}

# Month names: English, Devanagari, and the legacy "Kruti Dev" font's Latin-key encoding.
_MONTHS = {
    1: ("jan", "जनवरी", "tuojh"),
    2: ("feb", "फरवरी", "Qjojh"),
    3: ("mar", "मार्च", "ekpZ"),
    4: ("apr", "अप्रैल", "vizSy"),
    5: ("may", "मई", "ebZ"),
    6: ("jun", "जून", "twu"),
    7: ("jul", "जुलाई", "tqykbZ"),
    8: ("aug", "अगस्त", "vxLr"),
    9: ("sep", "सितम्बर", "flrEcj"),
    10: ("oct", "अक्टूबर", "vDVwcj"),
    11: ("nov", "नवम्बर", "uoEcj"),
    12: ("dec", "दिसम्बर", "fnlEcj"),
}
_DEV_FUZZY = {  # distinctive fragments that survive OCR errors
    1: "जनव",
    2: "फरव",
    3: "मार्च",
    4: "अप्र",
    5: "मई",
    6: "जून",
    7: "जुला",
    8: "अगस्",
    9: "सितम",
    10: "अक्ट",
    11: "नवम",
    12: "दिसम",
}
_YEAR = re.compile(r"(20[12]\d)")
_STAMP = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{4}|\d{2})(?!\d)")


@dataclass
class Report:
    path: Path
    month: date | None
    month_source: str
    rows: list[tuple[str, dict[str, float], int]] = field(
        default_factory=list
    )  # code, values, page
    method: str = ""
    notes: list[str] = field(default_factory=list)


def month_from_text(text: str) -> date | None:
    """Find 'month-year' in a report header (any of the three encodings)."""
    head = text[:1500]
    year = _YEAR.search(head)
    if not year:
        return None
    for m, (_, dev, kruti) in _MONTHS.items():
        if dev in head or _DEV_FUZZY[m] in head or re.search(rf"\b{re.escape(kruti)}\b", head):
            return date(int(year.group(1)), m, 1)
    return None


def month_from_name(name: str) -> date | None:
    """Fallback: reports are uploaded the month after sampling; names often carry ddmmyy."""
    low = Path(name).stem.lower()
    named = None
    for m, (eng, _, _) in _MONTHS.items():
        hit = re.search(rf"{eng}[a-z]*[-_ ]?(20[12]\d)?(?!\d)", low)
        if hit:
            if hit.group(1):
                return date(int(hit.group(1)), m, 1)
            named = m
            break
    s = _STAMP.search(low)
    if s:
        d, mo, y = int(s.group(1)), int(s.group(2)), int(s.group(3))
        y = y if y > 100 else 2000 + y
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2014 <= y <= 2030:
            if named:  # named month, year from the upload date
                return date(y - 1 if named > mo else y, named, 1)
            return date(y - 1, 12, 1) if mo == 1 else date(y, mo - 1, 1)
    year = _YEAR.search(low)
    if named and year:
        return date(int(year.group(1)), named, 1)
    return None


def upload_date(name: str) -> date | None:
    """The ddmmyy upload stamp in a file name, if any."""
    s = _STAMP.search(Path(name).stem.lower())
    if not s:
        return None
    d, mo, y = int(s.group(1)), int(s.group(2)), int(s.group(3))
    y = y if y > 100 else 2000 + y
    try:
        return date(y, mo, d) if 2014 <= y <= 2030 else None
    except ValueError:
        return None


def decide_month(rep: "Report", header: str) -> None:
    """Pick the report month from its header, checked against the file name.

    A month read from the header must not be after the upload date nor more than six months
    before it; otherwise the header was misread and the file name is used.
    """
    by_text, by_name = month_from_text(header), month_from_name(rep.path.name)
    up = upload_date(rep.path.name)
    if by_text and up:
        lag = (up.year - by_text.year) * 12 + up.month - by_text.month
        if not 0 <= lag <= 6:
            rep.notes.append(f"header read as {by_text:%Y-%m} but uploaded {up}; ignored")
            by_text = None
    if by_text:
        rep.month, rep.month_source = by_text, "report header"
        if by_name and by_name != by_text:
            rep.notes.append(f"file name suggests {by_name:%Y-%m}")
    elif by_name:
        rep.month, rep.month_source = by_name, "file name"


def _text_tokens(page) -> list[Token]:
    return [
        Token(
            w["text"], (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2, w["bottom"] - w["top"]
        )
        for w in page.extract_words(keep_blank_chars=False, use_text_flow=False)
    ]


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _ocr_tokens(gray) -> tuple[list[Token], object]:
    level = deskew(gray)
    _, _, mask = find_grid(level)
    clean = level.copy()
    clean[mask > 0] = 255
    result, _ = _engine()(cv2.cvtColor(clean, cv2.COLOR_GRAY2BGR))
    tokens = []
    for box, text, _conf in result or []:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        parts = text.split()
        # A detected box can span two cells ("9.40 1.1"); split it evenly along x.
        width = (max(xs) - min(xs)) / max(len(parts), 1)
        for i, part in enumerate(parts):
            tokens.append(Token(part, min(xs) + width * (i + 0.5), sum(ys) / 4, max(ys) - min(ys)))
    return tokens, clean


def _tess(img, lang: str, psm: int, whitelist: str | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.png"
        cv2.imwrite(str(src), img)
        cmd = ["tesseract", str(src), "stdout", "--psm", str(psm), "-l", lang]
        if whitelist:
            cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
        return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def _fix_decimals(rows: list[Row]) -> None:
    """DO and BOD are printed with a decimal point; OCR sometimes drops it ('1050' -> 10.50)."""
    for col, top in ((0, 25.0), (1, 60.0)):
        vals = [r.values[col] for r in rows if r.values[col] is not None]
        if not vals:
            continue
        typical = sorted(vals)[len(vals) // 2]
        for r in rows:
            v = r.values[col]
            if v is None or v <= top or v != int(v) or typical > top:
                continue
            for div in (10.0, 100.0, 1000.0):
                if v / div <= top:
                    r.values[col] = v / div
                    break


def _to_values(row: Row) -> dict[str, float]:
    out = {}
    for name, v in zip(VARIABLES, row.values, strict=True):
        lo, hi = LIMITS[name]
        if v is not None and lo <= v <= hi:
            out[name] = v
    # Fecal coliform is a subset of total coliform; a reversed pair means a misread.
    if "tc" in out and "fc" in out and out["fc"] > out["tc"] * 1.001:
        del out["tc"], out["fc"]
    return out


def _is_scan(pdf) -> bool:
    page = pdf.pages[0]
    big = [im for im in page.images if im["width"] * im["height"] > 0.5 * page.width * page.height]
    return bool(big) or len((page.extract_text() or "").strip()) < 200


def carve_images(data: bytes) -> list:
    """Grayscale page images recovered from the raw bytes of a damaged scanned PDF.

    Scans store each page as a JPEG. Complete ones decode normally; the last one in a cut-off
    file is decoded as far as it goes (the bottom of that page is lost).
    """
    import io

    import numpy as np
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    images = []
    for m in re.finditer(rb"\xff\xd8\xff", data):
        end = data.find(b"endstream", m.start())
        chunk = data[m.start() : end if end > 0 else len(data)]
        if len(chunk) < 50_000:
            continue
        try:
            img = Image.open(io.BytesIO(chunk))
            img.load()
            arr = np.asarray(img.convert("L"))
        except Exception:
            continue
        if end < 0:
            # Drop the undecoded tail, which PIL leaves as flat grey.
            flat = np.flatnonzero(arr.std(axis=1) > 2)
            if flat.size == 0:
                continue
            arr = arr[: flat[-1] + 1]
        if arr.shape[0] > 400 and arr.shape[1] > 400:
            images.append(np.ascontiguousarray(arr))
    return images


def _salvage(rep: Report, known_codes: set[str], why: str) -> Report:
    images = carve_images(rep.path.read_bytes())
    if not images:
        rep.notes.append(f"unreadable: {why}")
        return rep
    rep.method = "ocr-salvaged"
    rep.notes.append(f"damaged file ({why}): {len(images)} page image(s) recovered, rest lost")
    header = ""
    for i, gray in enumerate(images):
        # Scans are ~200 dpi; the OCR pipeline is tuned for 300.
        if gray.shape[1] < 2000:
            gray = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
        tokens, clean = _ocr_tokens(gray)
        if i == 0:
            header = _tess(clean[: clean.shape[0] // 5], "hin+eng", 6)
        rows = extract_rows(tokens, known_codes)
        _fix_decimals(rows)
        for r in rows:
            values = _to_values(r)
            if values:
                rep.rows.append((r.code, values, i + 1))
    if rep.month is None:
        decide_month(rep, header)
    return rep


MIN_TEXT_ROWS = 20


def _read_pages(rep: Report, pdf, known_codes: set[str], scan: bool) -> str:
    """Fill rep.rows from every page; returns the first page's header text."""
    rep.method = "ocr" if scan else "text"
    header = ""
    pages = render(rep.path) if scan else None
    for i, page in enumerate(pdf.pages):
        reader = None
        if scan:
            tokens, clean = _ocr_tokens(pages[i])
            if i == 0 and rep.month is None:
                header = _tess(clean[: clean.shape[0] // 5], "hin+eng", 6)

            def reader(x, y, h, img=clean):
                y0, y1 = int(max(0, y - 1.1 * h)), int(y + 1.1 * h)
                x0, x1 = int(max(0, x - 4 * h)), int(x + 4 * h)
                crop = cv2.copyMakeBorder(
                    img[y0:y1, x0:x1], 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255
                )
                digits = re.sub(r"\D", "", _tess(crop, "eng", 7, "0123456789"))
                return digits or None

        else:
            tokens = _text_tokens(page)
            if i == 0:
                header = page.extract_text() or ""
        rows = extract_rows(tokens, known_codes, read_code=reader)
        _fix_decimals(rows)
        for r in rows:
            values = _to_values(r)
            if values:
                rep.rows.append((r.code, values, i + 1))
    return header


def parse_report(path: Path, known_codes: set[str], month: date | None = None) -> Report:
    """Read every station row in one monthly report."""
    rep = Report(Path(path), month, "site label" if month else "")
    try:
        pdf = pdfplumber.open(path)
    except Exception as exc:  # corrupt or truncated download
        return _salvage(rep, known_codes, type(exc).__name__)
    if not pdf.pages:  # a cut-off file can open with no page tree at all
        pdf.close()
        return _salvage(rep, known_codes, "no pages")
    with pdf:
        scan = _is_scan(pdf)
        header = _read_pages(rep, pdf, known_codes, scan)
        if not scan and len(rep.rows) < MIN_TEXT_ROWS:
            # The text layer is unusable (legacy font or a poor embedded OCR layer):
            # read the rendered pages instead.
            text_header = header
            rep.rows.clear()
            header = _read_pages(rep, pdf, known_codes, True) or text_header
            if month_from_text(text_header):
                header = text_header
    if rep.month is None:
        decide_month(rep, header)
    seen, unique = set(), []
    for code, values, pg in rep.rows:
        if code not in seen:
            seen.add(code)
            unique.append((code, values, pg))
    if len(unique) != len(rep.rows):
        rep.notes.append(f"{len(rep.rows) - len(unique)} repeated station rows dropped")
    rep.rows = unique
    return rep
