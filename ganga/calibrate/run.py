"""Run calibration end to end and write the results files."""

import json
from datetime import date

import numpy as np

from ganga.calibrate import evaluate as ev
from ganga.calibrate import fit
from ganga.calibrate.data import VARIABLES, load_targets, predict, tributary_quality
from ganga.config import DATA_CURATED
from ganga.db import connect
from ganga.model.setup import load_forcing

START = date(2016, 1, 1)
SKILL = DATA_CURATED / "skill.json"


def build_context(end: date | None = None) -> fit.Context:
    with connect() as conn:
        last = conn.execute(
            "SELECT max(time) FROM timeseries WHERE source = 'era5' AND variable = 'rain'"
        ).fetchone()[0]
        end = end or last
        forcing, grid = load_forcing(conn, START, end, tributary=tributary_quality(conn))
        targets = load_targets(conn, grid)
        return fit.make_context(forcing, grid, targets, conn)


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def main(maxiter: int = 60, popsize: int = 8, workers: int = 12, log=print) -> dict:
    ctx = build_context()
    t = ctx.targets
    log(
        f"{len(t)} observations at {len(set(t.station))} stations; "
        f"{int(t.train.sum())} used for calibration"
    )
    result = fit.calibrate(ctx, maxiter=maxiter, popsize=popsize, workers=workers, log=log)
    sets = fit.behavioural(result)
    log(f"best score {result['score']:.4f}; {len(sets)} behavioural parameter sets")

    best_pred = predict(fit.run(ctx, result["x"]).conc, t, ctx.day_month)
    default_pred = predict(
        fit.run(ctx, np.array([np.mean(b) for b in fit.bounds()])).conc, t, ctx.day_month
    )
    sigma = ev.residual_sigma(best_pred, t)
    preds = np.stack([predict(fit.run(ctx, x).conc, t, ctx.day_month) for x in sets])
    base = ev.climatology_baseline(t, t.cell)
    report = {
        "created": date.today().isoformat(),
        "period": [ctx.grid.dates[0].isoformat(), ctx.grid.dates[-1].isoformat()],
        "split_date": "2023-01-01",
        "units": {"do": "mg/L", "bod": "mg/L", "fc": "log10 MPN/100 mL"},
        "counts": {
            "total": len(t),
            "train": int(t.train.sum()),
            "stations": len(set(t.station)),
            "unseen_stations": sorted(set(t.station[t.holdout_station])),
        },
        "model": ev.skill_table(best_pred, t),
        "climatology_baseline": ev.skill_table(base, t),
        "uncalibrated_midpoint": ev.skill_table(default_pred, t),
        "score_parts": fit.score_parts(ctx, result["x"]),
        "residual_sigma": dict(zip(VARIABLES, [float(s) for s in sigma], strict=True)),
        "coverage_90": {
            name: ev.interval_coverage(preds, sigma, t, mask) for name, mask in ev.splits(t).items()
        },
        "per_station": {},
    }
    for code in sorted(set(t.station), key=lambda c: t.cell[t.station == c][0]):
        s = t.station == code
        report["per_station"][code] = {
            "cell": int(t.cell[s][0]),
            "unseen": bool(t.holdout_station[s][0]),
            **{
                v: ev.metrics(best_pred[s & (t.variable == i)], t.value[s & (t.variable == i)])
                for i, v in enumerate(VARIABLES)
            },
        }
    fit.save(result, sets, {"residual_sigma": report["residual_sigma"]})
    SKILL.write_text(json.dumps(_jsonable(report), indent=1))
    return report


def summary(report: dict) -> str:
    lines = [
        f"{'split':16s} {'var':4s} {'n':>5s} {'model RMSE':>10s} {'baseline':>9s} "
        f"{'model NSE':>9s} {'bias':>6s} {'90% cover':>9s}"
    ]
    for split in ("train", "later_years", "unseen_stations"):
        for v in VARIABLES:
            m = report["model"][split][v]
            b = report["climatology_baseline"][split][v]
            if "rmse" not in m:
                continue
            cov = report["coverage_90"][split].get(v, float("nan"))
            lines.append(
                f"{split:16s} {v:4s} {m['n']:5d} {m['rmse']:10.2f} {b.get('rmse', 0):9.2f} "
                f"{m['nse']:9.2f} {m['bias']:6.2f} {cov:9.2f}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    rep = main(maxiter=iters)
    print(summary(rep))
