"""HTTP API for the dashboard. Run: uvicorn ganga.api.app:app --port 8010"""

import json
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ganga.config import DATA_CURATED, ROOT
from ganga.db import connect

app = FastAPI(title="Ganga water quality forecast", version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

VARIABLES = {
    "do": {"label": "Dissolved oxygen", "unit": "mg/L", "limit": 5.0, "bad": "below"},
    "bod": {"label": "BOD", "unit": "mg/L", "limit": 3.0, "bad": "above"},
    "fc": {"label": "Fecal coliform", "unit": "MPN/100 mL", "limit": 2500.0, "bad": "above"},
    "no3": {"label": "Nitrate-N", "unit": "mg/L", "confidence": "low"},
    "nh4": {"label": "Ammonia-N", "unit": "mg/L", "confidence": "low"},
    "tp": {"label": "Total phosphorus", "unit": "mg/L", "confidence": "low"},
    "flow": {"label": "River flow", "unit": "m3/s"},
    "p_unfit": {"label": "Chance of failing bathing limits", "unit": "probability"},
    "p_do": {"label": "Chance oxygen is below the limit", "unit": "probability"},
    "p_bod": {"label": "Chance BOD is above the limit", "unit": "probability"},
    "p_fc": {"label": "Chance coliform is above the limit", "unit": "probability"},
}
OBS_NAME = {"no3": "nitrate"}  # model variable -> observation variable


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def latest_run(conn) -> tuple[int, date, dict]:
    row = conn.execute(
        "SELECT id, issued_on, params, created_at FROM forecast_runs WHERE status = 'ok' "
        "ORDER BY issued_on DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise HTTPException(503, "no forecast has been issued yet")
    return row[0], row[1], {**(row[2] or {}), "created_at": row[3].isoformat()}


def resolve_chainage(conn, node: str | None, chainage: float | None) -> tuple[float, str]:
    if node:
        row = conn.execute(
            "SELECT chainage_km, name FROM nodes WHERE slug = %s", (node,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"unknown node {node}")
        return row[0], row[1]
    if chainage is None:
        raise HTTPException(422, "give node or chainage")
    length = conn.execute("SELECT max(chainage_km) FROM nodes").fetchone()[0]
    if not 0 <= chainage <= length:
        raise HTTPException(422, f"chainage must be between 0 and {length:.0f} km")
    near = conn.execute(
        "SELECT name, chainage_km FROM nodes WHERE kind = 'node' "
        "ORDER BY abs(chainage_km - %s) LIMIT 1",
        (chainage,),
    ).fetchone()
    if abs(chainage - near[1]) < 3:
        return chainage, near[0]
    side = "below" if chainage > near[1] else "above"
    return chainage, f"{abs(chainage - near[1]):.0f} km {side} {near[0]}"


@app.get("/api/meta")
def meta():
    with db() as conn:
        run_id, issued, params = latest_run(conn)
        length = conn.execute("SELECT max(chainage_km) FROM nodes").fetchone()[0]
        days = conn.execute(
            "SELECT min(time), max(time) FROM forecasts WHERE run_id = %s", (run_id,)
        ).fetchone()
    return {
        "run_id": run_id,
        "issued_on": issued.isoformat(),
        "run": params,
        "first_day": days[0].isoformat(),
        "last_day": days[1].isoformat(),
        "length_km": length,
        "variables": VARIABLES,
        "note": "Values are model estimates calibrated on monthly monitoring data, "
        "not sensor measurements.",
    }


@app.get("/api/nodes")
def nodes():
    with db() as conn:
        rows = conn.execute(
            "SELECT slug, name, kind, chainage_km, lon, lat FROM nodes "
            "WHERE kind IN ('node', 'confluence') ORDER BY chainage_km"
        ).fetchall()
    keys = ("slug", "name", "kind", "chainage_km", "lon", "lat")
    return [dict(zip(keys, r, strict=True)) for r in rows]


@app.get("/api/stations")
def stations():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.code, s.name, s.chainage_km, s.lon, s.lat,
                   (SELECT count(*) FROM obs_best o WHERE o.station_code = s.code
                      AND o.resolution IN ('month', 'day')) AS n
            FROM stations s WHERE s.chainage_km IS NOT NULL ORDER BY s.chainage_km
            """
        ).fetchall()
    keys = ("code", "name", "chainage_km", "lon", "lat", "observations")
    return [dict(zip(keys, r, strict=True)) for r in rows]


@app.get("/api/network")
def network():
    path = DATA_CURATED / "network.geojson"
    if not path.exists():
        raise HTTPException(503, "network not built")
    return json.loads(path.read_text())


@app.get("/api/river")
def river():
    """The river as one short line per model cell (matches /api/profile's chainages)."""
    path = DATA_CURATED / "river_cells.geojson"
    if not path.exists():
        raise HTTPException(503, "network not built")
    return json.loads(path.read_text())


@app.get("/api/loads")
def loads():
    import csv

    with (DATA_CURATED / "loads.csv").open() as fh:
        return list(csv.DictReader(fh))


@app.get("/api/forecast")
def forecast(
    node: str | None = None,
    chainage: float | None = None,
    variables: str = "do,bod,fc,flow,p_unfit",
):
    """Past ten days and the forecast at one place, with 5-95% bands."""
    wanted = [v for v in variables.split(",") if v in VARIABLES]
    with db() as conn:
        run_id, issued, _ = latest_run(conn)
        km, label = resolve_chainage(conn, node, chainage)
        cell_km = conn.execute(
            "SELECT chainage_km FROM forecasts WHERE run_id = %s "
            "ORDER BY abs(chainage_km - %s) LIMIT 1",
            (run_id, km),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT variable, time, p05, p50, p95 FROM forecasts WHERE run_id = %s "
            "AND chainage_km = %s AND variable = ANY(%s) ORDER BY time",
            (run_id, cell_km, wanted),
        ).fetchall()
        rain = conn.execute(
            """
            SELECT DISTINCT ON (t.time) t.time, t.value
            FROM timeseries t JOIN nodes n ON n.id = t.node_id
            WHERE t.variable = 'rain' AND n.kind = 'node'
              AND t.time BETWEEN %s AND %s
              AND n.id = (SELECT id FROM nodes WHERE kind = 'node'
                          ORDER BY abs(chainage_km - %s) LIMIT 1)
            ORDER BY t.time, CASE t.source WHEN 'era5' THEN 0 ELSE 1 END
            """,
            (issued - timedelta(days=10), issued + timedelta(days=5), km),
        ).fetchall()
    series: dict[str, list] = {v: [] for v in wanted}
    for variable, t, p05, p50, p95 in rows:
        series[variable].append(
            {"date": t.isoformat(), "p05": p05, "p50": p50, "p95": p95, "forecast": t > issued}
        )
    return {
        "place": label,
        "chainage_km": km,
        "issued_on": issued.isoformat(),
        "series": series,
        "rain": [{"date": t.isoformat(), "mm": v, "forecast": t > issued} for t, v in rain],
        "variables": {v: VARIABLES[v] for v in wanted},
    }


@app.get("/api/profile")
def profile(variable: str = "fc", day: date | None = None):
    """One variable along the whole river on one day (for colouring the map)."""
    if variable not in VARIABLES:
        raise HTTPException(422, "unknown variable")
    with db() as conn:
        run_id, issued, _ = latest_run(conn)
        day = day or issued
        rows = conn.execute(
            "SELECT chainage_km, p05, p50, p95 FROM forecasts WHERE run_id = %s "
            "AND variable = %s AND time = %s ORDER BY chainage_km",
            (run_id, variable, day),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "no data for that day in the latest run")
    return {
        "variable": variable,
        "day": day.isoformat(),
        "forecast": day > issued,
        "chainage_km": [r[0] for r in rows],
        "p05": [r[1] for r in rows],
        "p50": [r[2] for r in rows],
        "p95": [r[3] for r in rows],
        **VARIABLES[variable],
    }


@app.get("/api/observations")
def observations(
    station: str | None = None,
    chainage: float | None = None,
    variable: str = "fc",
    since: date = date(2016, 1, 1),
    within_km: float = Query(10.0, le=100.0),
):
    """Measured values at a station, or at stations near a chainage."""
    name = OBS_NAME.get(variable, variable)
    with db() as conn:
        if station:
            where, args = "s.code = %s", [station]
        elif chainage is not None:
            where, args = "abs(s.chainage_km - %s) <= %s", [chainage, within_km]
        else:
            raise HTTPException(422, "give station or chainage")
        rows = conn.execute(
            f"""
            SELECT o.station_code, s.name, s.chainage_km, o.time, o.resolution, o.value,
                   o.source_kind, o.source_key, o.source_page
            FROM obs_best o JOIN stations s ON s.code = o.station_code
            WHERE {where} AND o.variable = %s AND o.time >= %s
              AND o.resolution IN ('month', 'day')
            ORDER BY o.time
            """,
            (*args, name, since),
        ).fetchall()
    keys = (
        "station",
        "name",
        "chainage_km",
        "date",
        "resolution",
        "value",
        "source",
        "source_file",
        "page",
    )
    return [dict(zip(keys, (*r[:3], r[3].isoformat(), *r[4:]), strict=True)) for r in rows]


@app.get("/api/satellite")
def satellite(node: str):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT t.time, t.variable, t.value FROM timeseries t JOIN nodes n ON n.id = t.node_id
            WHERE n.slug = %s AND t.source = 'sentinel2' ORDER BY t.time
            """,
            (node,),
        ).fetchall()
    out: dict[str, dict] = {}
    for t, variable, value in rows:
        out.setdefault(t.isoformat(), {"date": t.isoformat()})[variable] = value
    return list(out.values())


@app.get("/api/alerts")
def alerts(days: int = Query(7, le=90), include_repeats: bool = False):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, kind, severity, chainage_km, valid_on, message, details
            FROM alerts WHERE created_at > now() - make_interval(days => %s)
              AND (%s OR NOT coalesce((details->>'repeat')::boolean, false))
            ORDER BY created_at DESC,
                     CASE severity WHEN 'severe' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END
            """,
            (days, include_repeats),
        ).fetchall()
    keys = ("id", "created_at", "kind", "severity", "chainage_km", "valid_on", "message", "details")
    return [
        dict(
            zip(
                keys,
                (
                    r[0],
                    r[1].isoformat(),
                    r[2],
                    r[3],
                    r[4],
                    r[5].isoformat() if r[5] else None,
                    r[6],
                    r[7],
                ),
                strict=True,
            )
        )
        for r in rows
    ]


@app.get("/api/skill")
def skill():
    path = DATA_CURATED / "skill.json"
    if not path.exists():
        raise HTTPException(503, "model has not been calibrated yet")
    return json.loads(path.read_text())


@app.get("/api/history")
def history(node: str | None = None, chainage: float | None = None):
    """Monthly modelled history at one place (the calibrated model's hindcast)."""
    import numpy as np

    path = DATA_CURATED / "hindcast.npz"
    if not path.exists():
        raise HTTPException(503, "historical simulation has not been generated")
    with db() as conn:
        km, label = resolve_chainage(conn, node, chainage)
    data = np.load(path)
    cell = int(np.argmin(np.abs(data["centres"] - km)))
    return {
        "place": label,
        "chainage_km": km,
        "months": [str(m) for m in data["months"]],
        "model": {
            v: [round(float(x), 3) for x in data[v][:, cell]]
            for v in ("do", "bod", "fc", "no3", "flow")
        },
    }


@app.get("/api/investment")
def investment():
    path = DATA_CURATED / "investment.json"
    if not path.exists():
        raise HTTPException(503, "investment analysis has not been generated")
    return json.loads(path.read_text())


class ScenarioIn(BaseModel):
    kind: str
    town: str | None = None
    percent: float = 0.0
    rain_mm: float = 0.0
    day_offset: int = 0
    tonnes_bod: float = 0.0
    chainage_km: float | None = None


@app.post("/api/scenario")
def scenario(body: ScenarioIn):
    from ganga import scenario as sc

    with db() as conn:
        _, issued, _ = latest_run(conn)
        try:
            return sc.run(
                conn,
                issued,
                body.kind,
                town=body.town or None,
                percent=body.percent,
                rain_mm=body.rain_mm,
                day_offset=body.day_offset,
                tonnes_bod=body.tonnes_bod,
                chainage_km=body.chainage_km,
            )
        except sc.ScenarioError as exc:
            raise HTTPException(422, str(exc)) from exc


class AskIn(BaseModel):
    question: str


@app.post("/api/ask")
def ask(body: AskIn):
    """Answer a plain-language question from the forecast data."""
    from ganga import assistant

    try:
        return assistant.ask(body.question)
    except LookupError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/health")
def health():
    with db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


def mount_frontend(application: FastAPI, dist: Path = ROOT / "web" / "dist") -> None:
    """Serve the built dashboard, if present, from the same origin as the API."""
    if not dist.exists():
        return
    application.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @application.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str):
        target = dist / path
        return FileResponse(target if path and target.is_file() else dist / "index.html")
