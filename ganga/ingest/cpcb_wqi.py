"""Parse a saved snapshot of CPCB's public "Suitability of River Ganga Water" map page.

The page embeds one map marker per monitoring station with its coordinates, code, name and the
latest month's values. We use it for station coordinates and as a monthly observation snapshot.
"""

import html
import re
from datetime import date, datetime
from pathlib import Path

_MARKER = re.compile(r"L\.marker\(\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\]")
_JOIN = re.compile(r"'\s*\+\s*'", re.S)
_HEAD = re.compile(r'<h5 class="head">\s*(\d+)\s*-\s*(.*?)</h5>', re.S)
_MONTH = re.compile(r"Sampling Month:\s*([A-Za-z]{3}-\d{4})")
_FIELDS = {
    "do": re.compile(r"Dissolved Oxygen:\s*([\d.]+)"),
    "bod": re.compile(r"Bio Chemical Oxygen Demand:\s*([\d.]+)"),
    "tc": re.compile(r"Total Coliform:\s*([\d.]+)"),
    "fc": re.compile(r"F(?:a)?ecal Coliform:\s*([\d.]+)"),
    "ph": re.compile(r"pH:\s*([\d.]+)"),
}
_STATE = re.compile(r",\s*([A-Za-z. ]+?)\s*$")


def parse_snapshot(text: str) -> list[dict]:
    """Return one dict per station: code, name, state, lon, lat, month (date|None), values{}."""
    out, seen = [], set()
    marks = list(_MARKER.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = _JOIN.sub("", text[m.end() : end])
        head = _HEAD.search(block)
        if not head:
            continue
        code, name = head.group(1), html.unescape(re.sub(r"\s+", " ", head.group(2))).strip()
        if code in seen:
            continue
        seen.add(code)
        mm = _MONTH.search(block)
        month: date | None = None
        if mm:
            month = datetime.strptime(mm.group(1), "%b-%Y").date()
        values = {}
        for key, pat in _FIELDS.items():
            v = pat.search(block)
            if v:
                try:
                    values[key] = float(v.group(1))
                except ValueError:
                    pass
        st = _STATE.search(name)
        out.append(
            {
                "code": code,
                "name": name,
                "state": st.group(1).strip() if st else None,
                "river": "Ganga" if "GANGA" in name.upper() else None,
                "lat": float(m.group(1)),
                "lon": float(m.group(2)),
                "month": month,
                "values": values,
            }
        )
    return out


def parse_file(path: Path) -> list[dict]:
    return parse_snapshot(Path(path).read_text(encoding="utf-8", errors="replace"))
