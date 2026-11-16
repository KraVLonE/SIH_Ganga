"""CPCB 'Status of Water Quality of River Ganga at Interstate Boundaries' reports.

One table per month. Newer files give a sampling date per row (about two per month); older ones
give a single monthly value. Columns are located by their header text.
"""

import re
from datetime import date, datetime
from pathlib import Path

import pdfplumber

_TITLE = re.compile(r"\(\s*([A-Za-z]+)\s*[-–,]?\s*(20\d\d)\s*\)")
_HEADERS = {
    "do": "dissolved",
    "ph": "ph",
    "conductivity": "conductivity",
    "bod": "bod",
    "fc": "fecal",
    "tc": "total",
}
_SCI = re.compile(r"^(\d+(?:\.\d+)?)\s*[×xX]\s*10\s*(\d)$")
_PLAIN = re.compile(r"^(\d+(?:\.\d+)?)")
_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})")


def parse_value(cell: str | None) -> tuple[float | None, bool]:
    """Number and below-detection flag. Handles '1(BDL)' and '28×103' (meaning 28 x 10^3)."""
    if not cell:
        return None, False
    text = cell.replace(",", "").replace("\n", " ").strip()
    bdl = "BDL" in text.upper()
    sci = _SCI.match(text)
    if sci:
        return float(sci.group(1)) * 10 ** int(sci.group(2)), bdl
    m = _PLAIN.match(text)
    return (float(m.group(1)), bdl) if m else (None, bdl)


def _month(text: str) -> date | None:
    m = _TITLE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)[:3]} {m.group(2)}", "%b %Y").date()
    except ValueError:
        return None


def _day(cell: str | None, month: date) -> date | None:
    m = _DATE.search(cell or "")
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y if y > 100 else 2000 + y
    try:
        got = date(y, mo, d)
    except ValueError:
        return None
    # Guard against typos: the sample must fall in or next to the report month.
    return got if abs((got - month).days) <= 45 else None


def parse_interstate(path: Path) -> list[dict]:
    """Rows: code, name, lat, lon, time, resolution ('day'|'month'), page, values{var: (v, bdl)}."""
    out = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            month = _month(page.extract_text() or "")
            if month is None:
                continue
            for table in page.extract_tables():
                cols, date_col, head_at = {}, None, None
                for i, row in enumerate(table[:4]):
                    cells = [(c or "").replace("\n", " ").lower() for c in row]
                    if any("station" in c and "code" in c for c in cells):
                        head_at = i
                        for var, key in _HEADERS.items():
                            for j, c in enumerate(cells):
                                if c.startswith(key) or (key == "bod" and c.startswith("bod")):
                                    cols[var] = j
                                    break
                        date_col = next((j for j, c in enumerate(cells) if "date" in c), None)
                        code_col = next(j for j, c in enumerate(cells) if "code" in c)
                        name_col = code_col + 1
                        break
                if head_at is None or "do" not in cols:
                    continue
                current = None
                for row in table[head_at + 1 :]:
                    code = (row[code_col] or "").strip()
                    if code.isdigit():
                        current = {
                            "code": code,
                            "name": re.sub(r"\s+", " ", row[name_col] or "").strip(),
                        }
                        nums = [c for c in row[name_col + 1 : name_col + 3] if c]
                        if len(nums) == 2 and all(re.match(r"^\d+\.\d+$", c.strip()) for c in nums):
                            current["lat"], current["lon"] = float(nums[0]), float(nums[1])
                    if current is None:
                        continue
                    values = {}
                    for var, j in cols.items():
                        v, bdl = parse_value(row[j] if j < len(row) else None)
                        if v is not None:
                            values[var] = (v, bdl)
                    if not values:
                        continue
                    day = _day(row[date_col], month) if date_col is not None else None
                    out.append(
                        {
                            **current,
                            "time": day or month,
                            "resolution": "day" if day else "month",
                            "page": pno,
                            "values": values,
                        }
                    )
    return out
