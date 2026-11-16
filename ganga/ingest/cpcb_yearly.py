"""CPCB yearly river water quality files: one row per station with the year's min and max.

Used for (a) the national station registry (code -> name, state) and (b) loose constraints on
variables that have no monthly data, notably nitrate.
"""

import re
from pathlib import Path

import pdfplumber

# Column pairs (min, max) after code, name, state - the 2023 layout.
PAIRS = ["water_temp", "do", "ph", "conductivity", "bod", "nitrate", "fc", "tc", "fs"]
_NUM = re.compile(r"^\d+(?:\.\d+)?$")
_BDL = re.compile(r"BDL", re.I)


def _num(cell: str | None) -> tuple[float | None, bool]:
    if not cell:
        return None, False
    text = cell.replace(",", "").strip()
    bdl = bool(_BDL.search(text))
    text = _BDL.sub("", text).strip("() ")
    return (float(text), bdl) if _NUM.match(text) else (None, bdl)


def parse_yearly(path: Path) -> list[dict]:
    """One dict per station row: code, name, state, page, and {variable: (min, max)}."""
    out = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables():
                for row in table:
                    if not row or not row[0] or not row[0].strip().isdigit():
                        continue
                    cells = [c for c in row]
                    if len(cells) != 3 + 2 * len(PAIRS):
                        continue
                    values = {}
                    for i, name in enumerate(PAIRS):
                        lo, _ = _num(cells[3 + 2 * i])
                        hi, _ = _num(cells[4 + 2 * i])
                        if lo is not None and hi is not None and lo <= hi:
                            values[name] = (lo, hi)
                    out.append(
                        {
                            "code": cells[0].strip(),
                            "name": re.sub(r"\s+", " ", cells[1] or "").strip(),
                            "state": re.sub(r"\s+", " ", cells[2] or "").strip().title(),
                            "page": pno,
                            "values": values,
                        }
                    )
    return out
