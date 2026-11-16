"""Build model forcing from the database and curated files."""

import csv
import json
from dataclasses import dataclass, replace
from datetime import date, timedelta

import numpy as np

from ganga.config import DATA_CURATED
from ganga.model.core import SPECIES, Forcing

DX_KM = 5.0
SATURATED = 99.0  # a DO value above saturation means "no deficit"

# Upstream boundary (Ganga at Haridwar): clean mountain water.
UPSTREAM = {"bod": 1.0, "nh4": 0.05, "tin": 0.4, "tp": 0.03, "fc": 60.0, "do": SATURATED}
# Unnamed gains between flow points (small streams, groundwater, drains).
BACKGROUND = {"bod": 2.0, "nh4": 0.2, "tin": 1.0, "tp": 0.08, "fc": 2000.0, "do": 7.0}
# Starting guesses for named tributaries; calibration replaces these where data exist.
TRIBUTARY = {
    "ramganga": {"bod": 4.0, "nh4": 0.6, "tin": 2.0, "tp": 0.15, "fc": 8000.0, "do": 6.5},
    "kali": {"bod": 15.0, "nh4": 3.0, "tin": 5.0, "tp": 0.5, "fc": 50000.0, "do": 3.0},
    "yamuna": {"bod": 3.0, "nh4": 0.4, "tin": 1.5, "tp": 0.1, "fc": 5000.0, "do": 7.0},
    "tons": {"bod": 2.0, "nh4": 0.2, "tin": 1.0, "tp": 0.08, "fc": 2000.0, "do": 7.0},
    "gomti": {"bod": 4.0, "nh4": 0.8, "tin": 2.0, "tp": 0.2, "fc": 10000.0, "do": 6.0},
    "ghaghara": {"bod": 2.0, "nh4": 0.2, "tin": 1.0, "tp": 0.08, "fc": 2000.0, "do": 7.5},
    "son": {"bod": 1.5, "nh4": 0.1, "tin": 0.8, "tp": 0.05, "fc": 1000.0, "do": 7.5},
}
# Approximate bed elevation along the river (chainage km, metres above sea level).
ELEVATION = [(0, 290), (245, 180), (600, 115), (840, 85), (1050, 75), (1369, 50)]
TRIB_SEARCH_KM = 60.0
WEATHER_VARS = ("air_temp", "solar", "rain")
WEATHER_SOURCES = ("openmeteo_forecast", "era5")  # later entries win where both exist


@dataclass
class Grid:
    dx_km: float
    centres: np.ndarray  # chainage of each cell centre
    dates: list[date]
    inflow_names: dict[int, str]  # cell -> tributary name or "local"
    town_names: list[str]
    layout: dict | None = None  # where flow steps sit along the river
    tributary: dict | None = None  # tributary quality used to build the forcing

    def cell(self, chainage_km: float) -> int:
        return int(np.clip(chainage_km // self.dx_km, 0, len(self.centres) - 1))

    def day(self, when: date) -> int:
        return (when - self.dates[0]).days


def _series(
    conn,
    variable: str,
    sources: tuple[str, ...],
    member: int,
    kinds: tuple[str, ...],
    start: date,
    end: date,
) -> tuple[np.ndarray, np.ndarray]:
    """(chainages, values[day, point]) for a variable; gaps are filled forward then backward."""
    rows = conn.execute(
        """
        SELECT n.chainage_km, t.time, t.value, t.source, t.member
        FROM timeseries t JOIN nodes n ON n.id = t.node_id
        WHERE t.variable = %s AND t.source = ANY(%s) AND t.member = ANY(%s)
          AND n.kind = ANY(%s) AND t.time BETWEEN %s AND %s
        """,
        (variable, list(sources), [0, member], list(kinds), start, end),
    ).fetchall()
    chain = sorted({r[0] for r in rows})
    col = {c: i for i, c in enumerate(chain)}
    n_days = (end - start).days + 1
    out = np.full((n_days, len(chain)), np.nan)
    rank = np.full((n_days, len(chain)), -1)
    for c, when, value, source, mem in rows:
        # Prefer the requested ensemble member, then the later-listed source.
        r = sources.index(source) + (10 if mem == member else 0)
        d, j = (when - start).days, col[c]
        if r > rank[d, j]:
            out[d, j], rank[d, j] = value, r
    for j in range(out.shape[1]):
        v = out[:, j]
        ok = ~np.isnan(v)
        if ok.any():
            v[:] = np.interp(np.arange(n_days), np.flatnonzero(ok), v[ok])
    return np.array(chain), out


LAYOUT_FILE = DATA_CURATED / "flow_layout.json"
LAYOUT_PERIOD = (date(2016, 1, 1), date(2025, 12, 31))


def flow_layout(conn, refresh: bool = False) -> dict:
    """Where along the river each flow step is placed, and which tributary it represents.

    The flow model's confluences sit a few cells away from the mapped ones, so each tributary
    is assigned to the nearby interval between flow points with the largest long-term gain.
    Computed once from a fixed reference period and cached, so short forecast windows and long
    calibration runs use the same layout.
    """
    if LAYOUT_FILE.exists() and not refresh:
        return json.loads(LAYOUT_FILE.read_text())
    fp_chain, fp_flow = _series(conn, "flow", ("glofas",), 0, ("node", "flowpoint"), *LAYOUT_PERIOD)
    mean_gain = np.diff(fp_flow.mean(axis=0))
    conf = conn.execute(
        "SELECT slug, chainage_km FROM nodes WHERE kind = 'confluence' ORDER BY chainage_km"
    ).fetchall()
    step_at = 0.5 * (fp_chain[:-1] + fp_chain[1:])
    names = ["local"] * len(step_at)
    for slug, ch in conf:
        near = [
            k
            for k in range(len(step_at))
            if fp_chain[k] - TRIB_SEARCH_KM <= ch <= fp_chain[k + 1] + TRIB_SEARCH_KM
            and names[k] == "local"
        ]
        if not near:
            continue
        k = max(near, key=lambda k: mean_gain[k])
        names[k] = slug
        step_at[k] = float(np.clip(ch, fp_chain[k] + 0.01, fp_chain[k + 1]))
    layout = {
        "fp_chain": [float(c) for c in fp_chain],
        "step_at": [float(s) for s in step_at],
        "names": names,
        "mean_gain": [float(g) for g in mean_gain],
    }
    LAYOUT_FILE.write_text(json.dumps(layout, indent=1))
    return layout


def flow_fields(
    fp_flow: np.ndarray, layout: dict, centres: np.ndarray, dates: list[date], tributary: dict
) -> tuple[np.ndarray, np.ndarray, dict, dict[int, str]]:
    """Cell flows, lateral inflows and their concentrations from flow at the flow points."""
    n_days, n = fp_flow.shape[0], len(centres)
    step_at, names = np.array(layout["step_at"]), layout["names"]
    flow = np.empty((n_days, n))
    inflow = np.zeros((n_days, n))
    conc = {s: np.zeros((n_days, n)) for s in (*SPECIES, "do")}
    inflow_names: dict[int, str] = {}
    edges = np.concatenate(([0.0], step_at, [np.inf]))
    for k in range(fp_flow.shape[1]):
        sel = (centres >= edges[k]) & (centres < edges[k + 1])
        flow[:, sel] = fp_flow[:, [k]]
    for k, name in enumerate(names):
        cell = int(np.clip(np.searchsorted(centres, step_at[k]), 0, n - 1))
        gain = np.maximum(fp_flow[:, k + 1] - fp_flow[:, k], 0.0)
        quality = {s: _by_day(v, dates) for s, v in tributary.get(name, BACKGROUND).items()}
        # Several steps can land in one cell: blend their concentrations by flow.
        total = inflow[:, cell] + gain
        for s in conc:
            with np.errstate(invalid="ignore", divide="ignore"):
                conc[s][:, cell] = np.where(
                    total > 0, (conc[s][:, cell] * inflow[:, cell] + quality[s] * gain) / total, 0.0
                )
        inflow[:, cell] = total
        if name != "local" or cell not in inflow_names:
            inflow_names[cell] = name
    return flow, inflow, conc, inflow_names


def member_flows(conn, start: date, end: date) -> dict[int, np.ndarray]:
    """Flow at the flow points for every ensemble member: member -> [day, point].

    Days a member does not cover (the past) take the deterministic series.
    """
    rows = conn.execute(
        """
        SELECT n.chainage_km, t.time, t.member, t.value
        FROM timeseries t JOIN nodes n ON n.id = t.node_id
        WHERE t.variable = 'flow' AND t.source = 'glofas'
          AND n.kind IN ('node', 'flowpoint') AND t.time BETWEEN %s AND %s
        """,
        (start, end),
    ).fetchall()
    chain = sorted({r[0] for r in rows})
    col = {c: i for i, c in enumerate(chain)}
    n_days = (end - start).days + 1
    members = sorted({r[2] for r in rows})
    data = {m: np.full((n_days, len(chain)), np.nan) for m in members}
    for c, when, m, v in rows:
        data[m][(when - start).days, col[c]] = v
    base = data[0]
    for j in range(base.shape[1]):
        ok = ~np.isnan(base[:, j])
        base[:, j] = np.interp(np.arange(n_days), np.flatnonzero(ok), base[ok, j])
    return {m: np.where(np.isnan(d), base, d) for m, d in data.items()}


def with_flow(forcing: Forcing, grid: Grid, fp_flow: np.ndarray) -> Forcing:
    """A copy of the forcing driven by a different flow series (e.g. an ensemble member)."""
    flow, inflow, conc, _ = flow_fields(
        fp_flow, grid.layout, grid.centres, grid.dates, grid.tributary
    )
    return replace(forcing, flow=flow, inflow=inflow, inflow_conc=conc)


def _by_day(value, dates: list[date]) -> np.ndarray | float:
    """A tributary concentration is either one number or twelve (one per calendar month)."""
    arr = np.asarray(value, dtype=float)
    if arr.ndim == 0:
        return float(arr)
    return arr[np.array([d.month - 1 for d in dates])]


def _towns() -> list[dict]:
    with (DATA_CURATED / "loads.csv").open() as fh:
        return list(csv.DictReader(fh))


def load_forcing(
    conn,
    start: date,
    end: date,
    member: int = 0,
    dx_km: float = DX_KM,
    tributary: dict[str, dict[str, float]] | None = None,
) -> tuple[Forcing, Grid]:
    """Forcing for [start, end]. member > 0 uses that flow-ensemble member where available."""
    tributary = {**TRIBUTARY, **(tributary or {})}
    n_days = (end - start).days + 1
    dates = [start + timedelta(days=i) for i in range(n_days)]
    length = conn.execute("SELECT max(chainage_km) FROM nodes").fetchone()[0]
    n = int(np.ceil(length / dx_km))
    centres = (np.arange(n) + 0.5) * dx_km

    # Flow at flow points -> a step function along the river; each step is a lateral inflow.
    fp_chain, fp_flow = _series(
        conn, "flow", ("glofas",), member, ("node", "flowpoint"), start, end
    )
    if fp_flow.size == 0 or np.isnan(fp_flow).any():
        raise RuntimeError("flow data missing for the requested period")
    layout = flow_layout(conn)
    flow, inflow, conc, inflow_names = flow_fields(fp_flow, layout, centres, dates, tributary)

    weather = {}
    for var in WEATHER_VARS:
        ch, vals = _series(conn, var, WEATHER_SOURCES, 0, ("node",), start, end)
        if vals.size == 0 or np.isnan(vals).all():
            raise RuntimeError(f"{var} data missing for the requested period")
        weather[var] = np.stack([np.interp(centres, ch, vals[d]) for d in range(n_days)])

    towns = _towns()
    untreated = np.array([float(t["untreated_mld"]) for t in towns])
    sewage = np.array([float(t["sewage_mld"]) for t in towns])
    used = np.array([float(t["stp_utilised_mld"]) for t in towns])
    pop = np.array(
        [
            float(t["population_lakh"]) if t["population_lakh"] else float(t["sewage_mld"]) / 10.8
            for t in towns
        ]
    )
    elev_ch, elev = zip(*ELEVATION, strict=True)
    forcing = Forcing(
        dx_km=dx_km,
        flow=flow,
        air_temp=weather["air_temp"],
        solar=weather["solar"],
        rain=weather["rain"],
        inflow=inflow,
        inflow_conc=conc,
        upstream={s: np.full(n_days, v) for s, v in UPSTREAM.items()},
        town_cell=np.array(
            [int(np.clip(float(t["chainage_km"]) // dx_km, 0, n - 1)) for t in towns]
        ),
        town_untreated=untreated,
        town_treated=np.minimum(used, sewage),
        town_population=pop,
        town_offset_km=np.array([float(t["offset_km"]) for t in towns]),
        town_scale=np.ones(len(towns)),
        elevation_m=np.interp(centres, elev_ch, elev),
        year=np.array([d.year + (d.timetuple().tm_yday - 1) / 365.25 for d in dates]),
    )
    grid = Grid(dx_km, centres, dates, inflow_names, [t["town"] for t in towns], layout, tributary)
    return forcing, grid
