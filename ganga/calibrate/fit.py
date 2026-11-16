"""Calibrate model parameters against observations with a global optimiser.

Score = sum over DO, BOD and log10(coliform) of mean squared error / observed variance
(so 1.0 per variable means "no better than the observed mean"), plus a lightly weighted
nitrate term from yearly ranges. Only training observations are scored.

Every parameter set the optimiser tries is recorded; the ones scoring close to the best form
the "behavioural" set used later for uncertainty bands.
"""

import json
import multiprocessing as mp
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

import numpy as np
from scipy.optimize import differential_evolution

from ganga.calibrate.data import VARIABLES, Targets, month_index, predict
from ganga.config import DATA_CURATED
from ganga.model.core import Forcing, Params, simulate
from ganga.model.setup import Grid

OUT = DATA_CURATED / "calibration.json"

# name, low, high, log-scale?
SPEC = [
    ("u_ref", 0.3, 1.0, False),
    ("kd", 0.03, 0.6, False),
    ("ks", 0.0, 0.3, False),
    ("kn", 0.05, 0.6, False),
    ("kfc", 0.1, 2.5, False),
    ("kfc_light", 0.0, 0.1, False),
    ("ka_scale", 0.3, 2.5, False),
    ("sod", 0.0, 2.5, False),
    ("photo", 0.0, 0.4, False),
    ("diffuse_bod", 1.0, 1500.0, True),
    ("diffuse_fc_ratio", 1e3, 3e5, True),
    ("diffuse_rain", 0.0, 0.3, False),
    ("load_scale", 0.3, 6.0, True),
    ("raw_fc", 1e6, 1e8, True),
    ("eff_fc", 1e3, 1e6, True),
    ("eff_bod", 10.0, 60.0, False),
    ("wash_bod", 0.0, 300.0, False),
    ("storm_bypass", 0.0, 0.1, False),
    ("load_2016", 1.0, 5.0, False),
    ("diffuse_2016", 1.0, 5.0, False),
    ("tw_a", 0.0, 10.0, False),
    ("tw_b", 0.5, 1.0, False),
]
# Extra multipliers for the three big urban clusters (on top of load_scale).
TOWN_GROUPS = {
    "kanpur": ("Kanpur Nagar", "Unnao NPP", "Shuklaganj NPP", "Bithoor"),
    "prayagraj": ("Prayagraj (M. Corp)",),
    "varanasi": ("Varanasi", "Mugalsarai"),
}
GROUP_BOUNDS = (0.2, 8.0)
NITRATE_WEIGHT = 0.3


@dataclass
class Context:
    forcing: Forcing
    grid: Grid
    targets: Targets
    day_month: np.ndarray
    variance: np.ndarray  # per variable, over training targets
    nitrate: tuple[np.ndarray, np.ndarray, np.ndarray] | None  # cell, year index, observed mid
    day_year: np.ndarray


_CTX: Context | None = None


def bounds() -> list[tuple[float, float]]:
    out = [(np.log10(lo), np.log10(hi)) if log else (lo, hi) for _, lo, hi, log in SPEC]
    return out + [tuple(np.log10(GROUP_BOUNDS))] * len(TOWN_GROUPS)


def decode(x: np.ndarray, grid: Grid) -> tuple[Params, np.ndarray]:
    """Optimiser vector -> (Params, per-town multipliers)."""
    kw = {name: (10.0 ** x[i] if log else float(x[i])) for i, (name, _, _, log) in enumerate(SPEC)}
    scale = np.ones(len(grid.town_names))
    for j, towns in enumerate(TOWN_GROUPS.values()):
        for k, name in enumerate(grid.town_names):
            if name in towns:
                scale[k] = 10.0 ** x[len(SPEC) + j]
    return Params().replace(**kw), scale


def describe(x: np.ndarray) -> dict[str, float]:
    out = {name: (10.0 ** x[i] if log else float(x[i])) for i, (name, _, _, log) in enumerate(SPEC)}
    for j, g in enumerate(TOWN_GROUPS):
        out[f"town_scale_{g}"] = 10.0 ** x[len(SPEC) + j]
    return out


def make_context(forcing: Forcing, grid: Grid, targets: Targets, conn=None) -> Context:
    day_month, _ = month_index(grid.dates)
    variance = np.array(
        [
            targets.value[targets.train & (targets.variable == i)].var()
            for i in range(len(VARIABLES))
        ]
    )
    years = np.array([d.year for d in grid.dates])
    day_year = years - years[0]
    nitrate = None
    if conn is not None:
        rows = conn.execute(
            """
            SELECT s.chainage_km, extract(year FROM a.time)::int, a.value, b.value
            FROM obs_best a JOIN obs_best b USING (station_code, variable, time)
            JOIN stations s ON s.code = a.station_code
            WHERE a.variable = 'nitrate' AND a.resolution = 'year_min'
              AND b.resolution = 'year_max' AND s.chainage_km IS NOT NULL AND b.value > 0
            """
        ).fetchall()
        full = {y for y in set(years) if (years == y).sum() >= 360}
        keep = [(grid.cell(c), y - years[0], 0.5 * (lo + hi)) for c, y, lo, hi in rows if y in full]
        if keep:
            nitrate = tuple(np.array(col) for col in zip(*keep, strict=True))
    return Context(forcing, grid, targets, day_month, variance, nitrate, day_year)


def run(ctx: Context, x: np.ndarray):
    params, scale = decode(x, ctx.grid)
    ctx.forcing.town_scale = scale
    return simulate(ctx.forcing, params)


def score_parts(ctx: Context, x: np.ndarray, mask: np.ndarray | None = None) -> dict[str, float]:
    res = run(ctx, x)
    pred = predict(res.conc, ctx.targets, ctx.day_month)
    mask = ctx.targets.train if mask is None else mask
    parts = {}
    for i, name in enumerate(VARIABLES):
        sel = mask & (ctx.targets.variable == i)
        parts[name] = float(np.mean((pred[sel] - ctx.targets.value[sel]) ** 2) / ctx.variance[i])
    if ctx.nitrate is not None:
        cell, yr, obs = ctx.nitrate
        n_years = ctx.day_year[-1] + 1
        sums = np.zeros((n_years, res.conc["no3"].shape[1]))
        np.add.at(sums, ctx.day_year, res.conc["no3"])
        annual = sums / np.bincount(ctx.day_year, minlength=n_years)[:, None]
        parts["nitrate"] = float(np.mean((annual[yr, cell] - obs) ** 2) / max(obs.var(), 1e-6))
    return parts


def score(x: np.ndarray) -> float:
    parts = score_parts(_CTX, x)
    total = sum(parts[v] for v in VARIABLES) + NITRATE_WEIGHT * parts.get("nitrate", 0.0)
    return total if np.isfinite(total) else 1e6


class _Recorder:
    """Evaluate a batch of vectors in worker processes and remember every result."""

    def __init__(self, pool):
        self.pool, self.xs, self.scores = pool, [], []

    def __call__(self, func, batch):
        batch = [np.asarray(b) for b in batch]
        out = self.pool.map(func, batch)
        self.xs.extend(batch)
        self.scores.extend(out)
        return out


def calibrate(
    ctx: Context, maxiter: int = 60, popsize: int = 8, workers: int = 12, seed: int = 1, log=print
) -> dict:
    global _CTX
    _CTX = ctx
    with mp.get_context("fork").Pool(workers) as pool:
        rec = _Recorder(pool)

        def progress(xk, convergence=0.0):
            log(f"  best so far {min(rec.scores):.4f} after {len(rec.scores)} runs")

        result = differential_evolution(
            score,
            bounds(),
            maxiter=maxiter,
            popsize=popsize,
            seed=seed,
            tol=1e-4,
            mutation=(0.5, 1.0),
            recombination=0.8,
            polish=False,
            updating="deferred",
            workers=rec,
            callback=progress,
            init="sobol",
        )
    xs, scores = np.array(rec.xs), np.array(rec.scores)
    order = np.argsort(scores)
    return {"x": result.x, "score": float(result.fun), "xs": xs[order], "scores": scores[order]}


def behavioural(
    fit: dict, tolerance: float = 0.05, max_sets: int = 200, min_sets: int = 30
) -> np.ndarray:
    """Distinct parameter sets scoring within `tolerance` of the best (at least min_sets)."""
    xs, scores = fit["xs"], fit["scores"]
    _, first = np.unique(np.round(xs, 6), axis=0, return_index=True)
    keep = np.sort(first)
    xs, scores = xs[keep], scores[keep]
    n = int(np.clip((scores <= scores[0] * (1 + tolerance)).sum(), min_sets, max_sets))
    return xs[:n]


def save(fit: dict, sets: np.ndarray, extra: dict, path: Path = OUT) -> None:
    path.write_text(
        json.dumps(
            {
                "created": date.today().isoformat(),
                "score": fit["score"],
                "best": describe(fit["x"]),
                "x": [float(v) for v in fit["x"]],
                "behavioural": [[float(v) for v in row] for row in sets],
                "spec": [list(s) for s in SPEC],
                "town_groups": {k: list(v) for k, v in TOWN_GROUPS.items()},
                **extra,
            },
            indent=1,
        )
    )


def load(path: Path = OUT) -> dict:
    data = json.loads(path.read_text())
    data["x"] = np.array(data["x"])
    data["behavioural"] = np.array(data["behavioural"])
    return data


def params_fields() -> list[str]:
    return [f.name for f in fields(Params)]
