"""What-if scenarios: re-run the model with one change and compare with the baseline."""

from dataclasses import replace
from datetime import date, timedelta

import numpy as np

from ganga.calibrate import fit
from ganga.calibrate.data import tributary_quality
from ganga.forecast.run import HORIZON_DAYS, SPINUP_DAYS
from ganga.model.core import DAY, Forcing, Params, simulate
from ganga.model.setup import Grid, flow_fields, load_forcing, member_flows

KINDS = ("plant_offline", "load_change", "rain", "low_flow", "spill")
SHOW = ("do", "bod", "fc")
RAIN_HALF_WIDTH_KM = 25.0


class ScenarioError(ValueError):
    pass


def _town(grid: Grid, name: str | None) -> int:
    if not name or name not in grid.town_names:
        raise ScenarioError(f"unknown town: {name!r}")
    return grid.town_names.index(name)


def _zeros(f: Forcing) -> dict[str, np.ndarray]:
    return {s: np.zeros(f.shape) for s in ("bod", "nh4", "tin", "tp", "fc")}


def apply(
    kind: str,
    f: Forcing,
    grid: Grid,
    p: Params,
    today_idx: int,
    *,
    town: str | None = None,
    percent: float = 0.0,
    rain_mm: float = 0.0,
    day_offset: int = 0,
    tonnes_bod: float = 0.0,
    chainage_km: float | None = None,
    fp_flow: np.ndarray | None = None,
) -> tuple[Forcing, str, int | None]:
    """Return (changed forcing, description, source cell)."""
    start = today_idx + max(0, min(int(day_offset), HORIZON_DAYS))
    when = grid.dates[start]
    if kind == "plant_offline":
        k = _town(grid, town)
        treated = f.town_treated[k]
        if treated <= 0:
            raise ScenarioError(f"{town} has no treatment in the inventory")
        # Treated flow is discharged raw instead: add the difference in strength.
        q = (
            treated
            * 1000.0
            / DAY
            * f.town_scale[k]
            * p.load_scale
            * np.exp(-f.town_offset_km[k] / p.delivery_km)
        )
        extra = _zeros(f)
        cell = int(f.town_cell[k])
        extra["bod"][start:, cell] = q * (p.raw_bod - p.eff_bod)
        extra["nh4"][start:, cell] = q * (p.raw_nh4 - p.eff_nh4)
        extra["tin"][start:, cell] = q * (p.raw_nh4 - p.eff_nh4 - p.eff_no3)
        extra["tp"][start:, cell] = q * (p.raw_tp - p.eff_tp)
        extra["fc"][start:, cell] = q * (p.raw_fc - p.eff_fc)
        return (
            replace(f, extra=extra),
            f"Treatment plants at {town} offline from {when:%d %b} ({treated:.0f} MLD untreated)",
            cell,
        )
    if kind == "load_change":
        if not -95 <= percent <= 300:
            raise ScenarioError("percent must be between -95 and 300")
        scale = f.town_scale.copy()
        if town:
            k = _town(grid, town)
            scale[k] *= 1 + percent / 100
            cell = int(f.town_cell[k])
            who = town
        else:
            scale *= 1 + percent / 100
            cell, who = None, "all towns"
        return (
            replace(f, town_scale=scale),
            f"Sewage load from {who} {'up' if percent > 0 else 'down'} {abs(percent):.0f}% "
            f"(sustained)",
            cell,
        )
    if kind == "rain":
        if not 0 < rain_mm <= 400:
            raise ScenarioError("rain_mm must be between 0 and 400")
        k = _town(grid, town)
        cell = int(f.town_cell[k])
        half = int(RAIN_HALF_WIDTH_KM / grid.dx_km)
        rain = f.rain.copy()
        rain[start, max(0, cell - half) : cell + half + 1] += rain_mm
        return (
            replace(f, rain=rain),
            f"{rain_mm:.0f} mm of rain over {town} on {when:%d %b}",
            cell,
        )
    if kind == "low_flow":
        if not -90 <= percent <= 100:
            raise ScenarioError("percent must be between -90 and 100")
        if fp_flow is None:
            raise ScenarioError("flow series unavailable")
        changed = fp_flow.copy()
        changed[today_idx:] *= 1 + percent / 100
        flow, inflow, conc, _ = flow_fields(
            changed, grid.layout, grid.centres, grid.dates, grid.tributary
        )
        return (
            replace(f, flow=flow, inflow=inflow, inflow_conc=conc),
            f"River flow {'up' if percent > 0 else 'down'} {abs(percent):.0f}% from today",
            None,
        )
    if kind == "spill":
        if chainage_km is None or not 0 <= chainage_km <= grid.centres[-1] + grid.dx_km:
            raise ScenarioError("chainage_km is outside the modelled river")
        if not 0 < tonnes_bod <= 5000:
            raise ScenarioError("tonnes_bod must be between 0 and 5000")
        cell = grid.cell(chainage_km)
        extra = _zeros(f)
        extra["bod"][start, cell] = tonnes_bod * 1e6 / DAY  # g/s over one day
        return (
            replace(f, extra=extra),
            f"{tonnes_bod:.0f} t BOD spilled at km {chainage_km:.0f} on {when:%d %b}",
            cell,
        )
    raise ScenarioError(f"unknown scenario kind: {kind}")


def run(conn, today: date, kind: str, calibration: dict | None = None, **kw) -> dict:
    calibration = calibration or fit.load()
    start, end = today - timedelta(days=SPINUP_DAYS), today + timedelta(days=HORIZON_DAYS)
    forcing, grid = load_forcing(conn, start, end, tributary=tributary_quality(conn))
    params, scale = fit.decode(calibration["x"], grid)
    forcing.town_scale = scale
    fp_flow = member_flows(conn, start, end)[0]
    today_idx = grid.day(today)
    changed, description, source = apply(
        kind, forcing, grid, params, today_idx, fp_flow=fp_flow, **kw
    )
    base = simulate(forcing, params, spinup_days=30)
    alt = simulate(changed, params, spinup_days=30)
    keep = slice(today_idx, None)
    places = conn.execute(
        "SELECT name, chainage_km FROM nodes WHERE kind = 'node' ORDER BY chainage_km"
    ).fetchall()
    travel = alt.travel_days[today_idx]
    arrivals = []
    for name, ch in places:
        c = grid.cell(ch)
        eta = float(travel[c] - travel[source]) if source is not None and c > source else None
        arrivals.append(
            {
                "name": name,
                "chainage_km": ch,
                "arrival_days": eta if eta is not None and eta <= 30 else None,
            }
        )
    summary = []
    last = -1
    for v, label, unit in (
        ("bod", "BOD", "mg/L"),
        ("fc", "Fecal coliform", "MPN/100 mL"),
        ("do", "Dissolved oxygen", "mg/L"),
    ):
        b, a = base.conc[v][keep], alt.conc[v][keep]
        diff = a - b
        d, c = np.unravel_index(
            np.argmin(diff) if v == "do" else np.argmax(np.abs(diff)), diff.shape
        )
        if abs(diff[d, c]) < (0.05 if v != "fc" else 1.0):
            continue
        near = min(places, key=lambda pl: abs(pl[1] - grid.centres[c]))[0]
        fmt = "{:,.0f}" if v == "fc" else "{:.1f}"
        summary.append(
            f"{label}: largest change near {near} on {grid.dates[today_idx + d]:%d %b}, "
            f"{fmt.format(b[d, c])} → {fmt.format(a[d, c])} {unit}"
        )
        last = max(last, int(d))
    if not summary:
        summary.append("No noticeable change within the 5-day horizon.")
    return {
        "description": description,
        "days": [d.isoformat() for d in grid.dates[keep]],
        "chainage_km": [float(c) for c in grid.centres],
        "baseline": {v: np.round(base.conc[v][keep], 3).tolist() for v in SHOW},
        "scenario": {v: np.round(alt.conc[v][keep], 3).tolist() for v in SHOW},
        "places": arrivals,
        "summary": summary,
    }
