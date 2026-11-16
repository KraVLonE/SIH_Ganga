"""Load parsed observations into the database, with their source file recorded."""

from datetime import date
from pathlib import Path

from ganga.config import DATA_RAW, ROOT

# Plausible physical ranges; anything outside is a misread and is not loaded.
RANGES = {
    "do": (0.0, 25.0),
    "bod": (0.0, 400.0),
    "fc": (1.0, 1e9),
    "tc": (1.0, 1e9),
    "ph": (3.0, 11.0),
    "conductivity": (10.0, 60000.0),
    "nitrate": (0.0, 200.0),
    "water_temp": (0.0, 45.0),
    "fs": (0.0, 1e9),
}


def source_id(conn, key: str, kind: str, url: str | None = None, note: str | None = None) -> int:
    return conn.execute(
        """
        INSERT INTO sources (key, kind, url, note) VALUES (%s, %s, %s, %s)
        ON CONFLICT (key) DO UPDATE SET kind = EXCLUDED.kind,
            url = COALESCE(EXCLUDED.url, sources.url), note = EXCLUDED.note
        RETURNING id
        """,
        (key, kind, url, note),
    ).fetchone()[0]


def ensure_station(conn, code: str, registry: dict[str, dict], name: str | None = None) -> None:
    info = registry.get(code, {})
    conn.execute(
        "INSERT INTO stations (code, name, state) VALUES (%s, %s, %s) "
        "ON CONFLICT (code) DO NOTHING",
        (code, info.get("name") or name or f"Station {code}", info.get("state")),
    )


def add(
    conn,
    sid: int,
    code: str,
    variable: str,
    when: date,
    resolution: str,
    value: float,
    page: int | None = None,
    bdl: bool = False,
) -> bool:
    lo, hi = RANGES.get(variable, (float("-inf"), float("inf")))
    if not lo <= value <= hi:
        return False
    conn.execute(
        """
        INSERT INTO observations (station_code, variable, time, resolution, value,
                                  below_detection, source_id, source_page)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (station_code, variable, time, resolution, source_id)
        DO UPDATE SET value = EXCLUDED.value, below_detection = EXCLUDED.below_detection,
            source_page = EXCLUDED.source_page
        """,
        (code, variable, when, resolution, value, bdl, sid, page),
    )
    return True


def rel(path: Path) -> str:
    return str(Path(path).resolve().relative_to(ROOT))


def load_uppcb(conn, registry: dict[str, dict]) -> dict:
    """Monthly UP reports. If two files claim the same month, the one with more rows wins."""
    from ganga.ingest.uppcb_run import load_parsed

    reports = [r for r in load_parsed() if r["month"] and r["rows"]]
    best: dict[str, dict] = {}
    for r in reports:
        cur = best.get(r["month"])
        better = cur is None or (
            (r["month_source"] == "site label", len(r["rows"]))
            > (cur["month_source"] == "site label", len(cur["rows"]))
        )
        if better:
            best[r["month"]] = r
    conn.execute(
        "DELETE FROM observations WHERE source_id IN "
        "(SELECT id FROM sources WHERE kind = 'uppcb_monthly')"
    )
    n = 0
    for month, r in sorted(best.items()):
        sid = source_id(
            conn,
            f"data/raw/uppcb/{r['file']}",
            "uppcb_monthly",
            r["url"],
            f"{r['method']}; month from {r['month_source']}",
        )
        for row in r["rows"]:
            ensure_station(conn, row["code"], registry)
            for var in ("do", "bod", "tc", "fc"):
                if var in row:
                    n += add(
                        conn,
                        sid,
                        row["code"],
                        var,
                        date.fromisoformat(month),
                        "month",
                        row[var],
                        row["page"],
                    )
    conn.commit()
    return {"months": len(best), "values": n, "files_with_rows": len(reports)}


def load_interstate(conn, registry: dict[str, dict]) -> dict:
    from ganga.ingest.cpcb_interstate import parse_interstate

    n, files = 0, 0
    for path in sorted((DATA_RAW / "cpcb" / "interstate").glob("*.pdf")):
        try:
            rows = parse_interstate(path)
        except Exception:
            continue
        if not rows:
            continue
        files += 1
        sid = source_id(
            conn, rel(path), "cpcb_interstate", f"https://cpcb.nic.in/NGTMC/{path.name}"
        )
        for r in rows:
            ensure_station(conn, r["code"], registry, r["name"])
            for var, (v, bdl) in r["values"].items():
                n += add(conn, sid, r["code"], var, r["time"], r["resolution"], v, r["page"], bdl)
    conn.commit()
    return {"files": files, "values": n}


def load_yearly(conn, registry: dict[str, dict]) -> dict:
    import re

    from ganga.ingest.cpcb_yearly import parse_yearly

    n, files = 0, 0
    for path in sorted((DATA_RAW / "cpcb" / "nwmp").glob("*.pdf")):
        year = re.search(r"(20\d\d)", path.name)
        if not year:
            continue
        rows = parse_yearly(path)
        if not rows:
            continue
        files += 1
        when = date(int(year.group(1)), 1, 1)
        sid = source_id(conn, rel(path), "cpcb_yearly")
        for r in rows:
            ensure_station(conn, r["code"], registry, r["name"])
            for var, (lo, hi) in r["values"].items():
                n += add(conn, sid, r["code"], var, when, "year_min", lo, r["page"])
                n += add(conn, sid, r["code"], var, when, "year_max", hi, r["page"])
    conn.commit()
    return {"files": files, "values": n}


def load_wqi(conn, registry: dict[str, dict]) -> dict:
    from ganga.ingest.cpcb_wqi import parse_file

    n, files = 0, 0
    for path in sorted((DATA_RAW / "cpcb" / "wqi").glob("*.html")):
        files += 1
        sid = source_id(conn, rel(path), "cpcb_wqi_map", "https://ewqdes.cpcb.gov.in/wqi/")
        for s in parse_file(path):
            if s["month"] is None:
                continue
            ensure_station(conn, s["code"], registry, s["name"])
            for var, v in s["values"].items():
                n += add(conn, sid, s["code"], var, s["month"], "month", v)
    conn.commit()
    return {"files": files, "values": n}
