"""Parse every UP monthly report in the manifest, caching one JSON per file.

OCR takes ~40 s per scanned report, so results are cached in data/curated/uppcb_parsed/ and
only missing files are parsed.
"""

import csv
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ganga.config import DATA_CURATED, DATA_RAW
from ganga.ingest.uppcb_manifest import month_of, path_of, read_manifest

PARSED = DATA_CURATED / "uppcb_parsed"
REGISTRY = DATA_CURATED / "station_registry.csv"
YEARLY_2023 = DATA_RAW / "cpcb" / "nwmp" / "WQuality_River-Data-2023.pdf"


def build_registry() -> dict[str, dict]:
    """National station list from the CPCB yearly file, plus the Ganga map stations."""
    from ganga.ingest.cpcb_wqi import parse_file
    from ganga.ingest.cpcb_yearly import parse_yearly

    reg = {
        r["code"]: {"code": r["code"], "name": r["name"], "state": r["state"]}
        for r in parse_yearly(YEARLY_2023)
    }
    snaps = sorted((DATA_RAW / "cpcb" / "wqi").glob("*.html"))
    if snaps:
        for s in parse_file(snaps[-1]):
            reg.setdefault(s["code"], {"code": s["code"], "name": s["name"], "state": s["state"]})
    with REGISTRY.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["code", "name", "state"])
        w.writeheader()
        w.writerows(sorted(reg.values(), key=lambda r: int(r["code"])))
    return reg


def read_registry() -> dict[str, dict]:
    if not REGISTRY.exists():
        return build_registry()
    with REGISTRY.open() as fh:
        return {r["code"]: r for r in csv.DictReader(fh)}


def _one(args: tuple[dict, list[str]]) -> str:
    from ganga.ingest.uppcb import parse_report

    row, codes = args
    out = PARSED / (row["file"] + ".json")
    try:
        rep = parse_report(path_of(row), set(codes), month_of(row))
    except Exception as exc:  # one bad file must not stop the batch
        from ganga.ingest.uppcb import Report

        rep = Report(path_of(row), month_of(row), "", notes=[f"parser error: {exc!r}"])
    out.write_text(
        json.dumps(
            {
                "file": row["file"],
                "url": row["url"],
                "month": rep.month.isoformat() if rep.month else None,
                "month_source": rep.month_source,
                "method": rep.method,
                "notes": rep.notes,
                "rows": [{"code": c, "page": p, **v} for c, v, p in rep.rows],
            },
            indent=1,
        )
    )
    return f"{row['file']}: {len(rep.rows)} rows, month {rep.month} ({rep.month_source})"


def parse_all(workers: int = 4, log=print) -> None:
    PARSED.mkdir(parents=True, exist_ok=True)
    codes = sorted(read_registry())
    todo = [
        (r, codes)
        for r in read_manifest()
        if path_of(r).exists()
        and Path(str(path_of(r)) + ".ok").exists()
        and not (PARSED / (r["file"] + ".json")).exists()
    ]
    log(f"{len(todo)} reports to parse")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for msg in pool.map(_one, todo):
            log(msg)


def load_parsed() -> list[dict]:
    """Cached parse results, with header-derived months re-checked against upload dates."""
    from datetime import date

    from ganga.ingest.uppcb import month_from_name, upload_date

    out = []
    for p in sorted(PARSED.glob("*.json")):
        r = json.loads(p.read_text())
        up = upload_date(r["file"])
        if r["month"] and r["month_source"] == "report header" and up:
            m = date.fromisoformat(r["month"])
            lag = (up.year - m.year) * 12 + up.month - m.month
            if not 0 <= lag <= 6:
                by_name = month_from_name(r["file"])
                r["notes"].append(f"header read as {m:%Y-%m} but uploaded {up}; ignored")
                r["month"] = by_name.isoformat() if by_name else None
                r["month_source"] = "file name" if by_name else ""
        out.append(r)
    return out
