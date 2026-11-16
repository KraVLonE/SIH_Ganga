"""Load flow and weather drivers into the timeseries table. All writes are upserts."""

from collections.abc import Iterable
from datetime import date, timedelta

from ganga.ingest.openmeteo import WEATHER_FIELDS, OpenMeteo, Row, parse_daily

FLOW_RENAME = {"river_discharge": "flow"}
WEATHER_RENAME = {v: k for k, v in WEATHER_FIELDS.items()}
ERA5_LAG_DAYS = 7  # reanalysis trails real time; the forecast model fills the gap
CELL = 0.05  # GloFAS grid spacing, degrees


def upsert(conn, node_ids: list[int], source: str, rows: Iterable[Row]) -> int:
    data = [(node_ids[i], var, source, member, day, value) for i, var, member, day, value in rows]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO timeseries (node_id, variable, source, member, time, value)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (node_id, variable, source, member, time)
            DO UPDATE SET value = EXCLUDED.value
            """,
            data,
        )
    conn.commit()
    return len(data)


def _nodes(conn, kinds: tuple[str, ...]) -> list[tuple]:
    return conn.execute(
        "SELECT id, slug, lon, lat, flow_lon, flow_lat FROM nodes WHERE kind = ANY(%s) "
        "ORDER BY chainage_km",
        (list(kinds),),
    ).fetchall()


FLOW_KINDS = ("node", "flowpoint")


def pick_flow_cells(conn, om: OpenMeteo, start: date, end: date) -> list[tuple[str, float]]:
    """GloFAS is a 5 km grid; the cell nearest a node may be floodplain, not the channel.

    For each node, try the 3x3 block of cells around it and keep the one with the largest mean
    discharge over a monsoon window. Returns (slug, mean flow) for inspection.
    """
    out = []
    offsets = [(dx * CELL, dy * CELL) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
    for node_id, slug, lon, lat, _, _ in _nodes(conn, FLOW_KINDS):
        pts = [(lon + dx, lat + dy) for dx, dy in offsets]
        payload = om.flow_history(pts, start, end)
        means: dict[int, list[float]] = {}
        for i, _, _, _, v in parse_daily(payload, FLOW_RENAME):
            means.setdefault(i, []).append(v)
        if not means:
            raise RuntimeError(f"no discharge returned around {slug}")
        best = max(means, key=lambda i: sum(means[i]) / len(means[i]))
        mean = sum(means[best]) / len(means[best])
        # Store the point we asked for, not the cell centre the API reports, so the same
        # request reproduces the same cell.
        conn.execute(
            "UPDATE nodes SET flow_lon = %s, flow_lat = %s WHERE id = %s",
            (pts[best][0], pts[best][1], node_id),
        )
        conn.commit()
        out.append((slug, mean))
    return out


def _flow_points(conn) -> tuple[list[int], list[tuple[float, float]]]:
    rows = _nodes(conn, FLOW_KINDS)
    missing = [r[1] for r in rows if r[4] is None]
    if missing:
        raise RuntimeError(f"run pick-cells first; no flow cell for: {', '.join(missing)}")
    return [r[0] for r in rows], [(r[4], r[5]) for r in rows]


def _weather_points(conn) -> tuple[list[int], list[tuple[float, float]]]:
    rows = _nodes(conn, ("node",))
    return [r[0] for r in rows], [(r[2], r[3]) for r in rows]


def _year_chunks(start: date, end: date):
    y = start.year
    while y <= end.year:
        a, b = max(start, date(y, 1, 1)), min(end, date(y, 12, 31))
        yield a, b
        y += 1


def _have(conn, variable: str, source: str, a: date, b: date, n_nodes: int) -> bool:
    """True if every node already has a full set of days for this window."""
    days = (b - a).days + 1
    got = conn.execute(
        "SELECT count(*) FROM timeseries WHERE variable = %s AND source = %s AND member = 0 "
        "AND time BETWEEN %s AND %s",
        (variable, source, a, b),
    ).fetchone()[0]
    return got >= days * n_nodes


def backfill(conn, om: OpenMeteo, start: date, today: date, log=print) -> None:
    """History for all nodes, one calendar year per request batch; skips complete years."""
    ids, pts = _flow_points(conn)
    flow_end = today - timedelta(days=1)
    for a, b in _year_chunks(start, flow_end):
        if _have(conn, "flow", "glofas", a, b, len(ids)):
            continue
        n = upsert(conn, ids, "glofas", parse_daily(om.flow_history(pts, a, b), FLOW_RENAME))
        log(f"flow {a}..{b}: {n} rows")
    ids, pts = _weather_points(conn)
    wx_end = today - timedelta(days=ERA5_LAG_DAYS)
    for a, b in _year_chunks(start, wx_end):
        if _have(conn, "rain", "era5", a, b, len(ids)):
            continue
        n = upsert(conn, ids, "era5", parse_daily(om.weather_history(pts, a, b), WEATHER_RENAME))
        log(f"weather {a}..{b}: {n} rows")


def daily(conn, om: OpenMeteo, log=print) -> None:
    """Recent days plus the forecast: flow with its ensemble, weather from the forecast model."""
    ids, pts = _flow_points(conn)
    n = upsert(conn, ids, "glofas", parse_daily(om.flow_forecast(pts), FLOW_RENAME))
    log(f"flow recent+forecast: {n} rows")
    ids, pts = _weather_points(conn)
    n = upsert(
        conn, ids, "openmeteo_forecast", parse_daily(om.weather_forecast(pts), WEATHER_RENAME)
    )
    log(f"weather recent+forecast: {n} rows")
