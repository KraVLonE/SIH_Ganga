"""Issue a forecast: run the calibrated model as an ensemble and store the summary.

Ensemble = behavioural parameter sets x river-flow forecast members, plus the residual
noise seen in calibration. Stored per cell and day as 5th / 50th / 95th percentiles, with
the probability of breaching each bathing-water limit.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from ganga.calibrate import fit
from ganga.calibrate.data import VARIABLES, tributary_quality
from ganga.model.core import Result, simulate
from ganga.model.setup import Grid, load_forcing, member_flows, with_flow

MODEL_VERSION = "0.1"
SPINUP_DAYS = 150  # longer than the slowest dry-season travel time down the river
HISTORY_DAYS = 10
HORIZON_DAYS = 5
MAX_PARAM_SETS = 24
MEMBERS_PER_SET = 4
NOISE_DRAWS = 8  # per ensemble member; 96 members x 8 = 768 draws per cell-day
OUTPUTS = ("do", "bod", "fc", "no3", "nh4", "tp")
# Primary water quality criteria for outdoor bathing (MoEF&CC).
LIMITS = {"do": ("below", 5.0), "bod": ("above", 3.0), "fc": ("above", 2500.0)}


@dataclass
class Ensemble:
    grid: Grid
    keep: slice  # days of the run that are reported
    dates: list[date]
    conc: dict[str, np.ndarray]  # variable -> [member, day, cell]
    flow: np.ndarray  # [member, day, cell]
    rain: np.ndarray  # [day, cell]
    travel: np.ndarray  # [day, cell] cumulative travel days (median parameter set)
    sigma: dict[str, float]  # residual spread; coliform in log10 units


def run_ensemble(
    conn,
    today: date,
    calibration: dict | None = None,
    seed: int = 0,
    extra: dict | None = None,
    modify=None,
) -> Ensemble:
    """Simulate [today - SPINUP, today + HORIZON] for many parameter sets and flow members.

    `extra` adds loads (species -> [day, cell]) and `modify(forcing, grid)` may alter the
    forcing in place; both are used by scenarios.
    """
    calibration = calibration or fit.load()
    start, end = today - timedelta(days=SPINUP_DAYS), today + timedelta(days=HORIZON_DAYS)
    forcing, grid = load_forcing(conn, start, end, tributary=tributary_quality(conn))
    if extra:
        forcing.extra = extra
    if modify:
        modify(forcing, grid)
    flows = member_flows(conn, start, end)
    members = [m for m in sorted(flows) if m > 0] or [0]
    sets = calibration["behavioural"][:MAX_PARAM_SETS]
    rng = np.random.default_rng(seed)
    keep = slice(SPINUP_DAYS - HISTORY_DAYS, None)
    conc = {v: [] for v in OUTPUTS}
    flow, travel = [], None
    for i, x in enumerate(sets):
        params, scale = fit.decode(x, grid)
        for m in rng.choice(members, size=min(MEMBERS_PER_SET, len(members)), replace=False):
            f = with_flow(forcing, grid, flows[int(m)])
            f.town_scale = scale * (forcing.town_scale if forcing.town_scale.size else 1.0)
            res: Result = simulate(f, params, spinup_days=30)
            for v in OUTPUTS:
                conc[v].append(res.conc[v][keep])
            flow.append(f.flow[keep])
            if i == 0 and travel is None:
                travel = res.travel_days[keep]
    sigma = calibration.get("residual_sigma", {})
    return Ensemble(
        grid,
        keep,
        grid.dates[keep],
        {v: np.stack(a) for v, a in conc.items()},
        np.stack(flow),
        forcing.rain[keep],
        travel,
        {v: float(sigma.get(v, 0.0)) for v in VARIABLES},
    )


def with_noise(ens: Ensemble, variable: str, seed: int = 0) -> np.ndarray:
    """Ensemble values with calibration residual noise added: [draw, day, cell]."""
    x = ens.conc[variable]
    s = ens.sigma.get(variable, 0.0)
    if s <= 0:
        return x
    rng = np.random.default_rng(seed)
    # One noise value per draw and cell-day keeps bands honest for single-point reads.
    if variable == "fc":
        noisy = np.log10(np.maximum(x, 1.0))[:, None] + rng.normal(
            0, s, (x.shape[0], NOISE_DRAWS, *x.shape[1:])
        )
        return (10.0**noisy).reshape(-1, *x.shape[1:])
    noisy = x[:, None] + rng.normal(0, s, (x.shape[0], NOISE_DRAWS, *x.shape[1:]))
    return np.maximum(noisy, 0.0).reshape(-1, *x.shape[1:])


def summarise(ens: Ensemble) -> dict[str, dict[str, np.ndarray]]:
    """variable -> {p05, p50, p95} as [day, cell]; plus breach probabilities."""
    out = {}
    unfit = None
    for v in OUTPUTS:
        vals = with_noise(ens, v)
        p05, p50, p95 = np.quantile(vals, [0.05, 0.5, 0.95], axis=0)
        out[v] = {"p05": p05, "p50": np.median(ens.conc[v], axis=0), "p95": p95}
        if v in LIMITS:
            side, limit = LIMITS[v]
            bad = vals < limit if side == "below" else vals > limit
            out[f"p_{v}"] = {"p50": bad.mean(axis=0)}
            unfit = bad if unfit is None else (unfit[: len(bad)] | bad[: len(unfit)])
    out["p_unfit"] = {"p50": unfit.mean(axis=0)}
    q05, q50, q95 = np.quantile(ens.flow, [0.05, 0.5, 0.95], axis=0)
    out["flow"] = {"p05": q05, "p50": q50, "p95": q95}
    return out


def store(conn, today: date, ens: Ensemble, summary: dict, calibration: dict) -> int:
    run_id = conn.execute(
        "INSERT INTO forecast_runs (issued_on, model_version, params) VALUES (%s, %s, %s) "
        "RETURNING id",
        (
            today,
            MODEL_VERSION,
            json.dumps(
                {
                    "calibrated": calibration.get("created"),
                    "score": calibration.get("score"),
                    "members": int(ens.flow.shape[0]),
                    "history_days": HISTORY_DAYS,
                    "horizon_days": HORIZON_DAYS,
                    "dx_km": ens.grid.dx_km,
                }
            ),
        ),
    ).fetchone()[0]
    rows = []
    for variable, parts in summary.items():
        p50 = parts["p50"]
        p05, p95 = parts.get("p05"), parts.get("p95")
        for d, when in enumerate(ens.dates):
            for c, chainage in enumerate(ens.grid.centres):
                rows.append(
                    (
                        run_id,
                        float(chainage),
                        variable,
                        when,
                        None if p05 is None else float(p05[d, c]),
                        float(p50[d, c]),
                        None if p95 is None else float(p95[d, c]),
                    )
                )
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY forecasts (run_id, chainage_km, variable, time, p05, p50, p95) FROM STDIN"
        ) as copy,
    ):
        for r in rows:
            copy.write_row(r)
    conn.commit()
    return run_id


def issue(conn, today: date | None = None, log=print) -> int:
    """Run and store today's forecast, then raise alerts. Returns the run id."""
    from ganga.alerts.rules import evaluate, record

    today = today or date.today()
    calibration = fit.load()
    ens = run_ensemble(conn, today, calibration)
    summary = summarise(ens)
    run_id = store(conn, today, ens, summary, calibration)
    alerts = evaluate(conn, today, ens, summary)
    n_new = record(conn, run_id, alerts)
    log(
        f"forecast run {run_id} issued for {today}: {ens.flow.shape[0]} members, "
        f"{len(alerts)} alerts ({n_new} new)"
    )
    return run_id
