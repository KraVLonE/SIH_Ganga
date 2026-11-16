"""Historical simulation with the calibrated model, and the investment before/after analysis."""

import json
from dataclasses import replace
from datetime import date

import numpy as np

from ganga.calibrate import fit
from ganga.calibrate.data import month_index, monthly_mean
from ganga.calibrate.run import build_context
from ganga.config import DATA_CURATED

HINDCAST = DATA_CURATED / "hindcast.npz"
INVESTMENT = DATA_CURATED / "investment.json"
VARS = ("do", "bod", "fc", "no3")
# Urban cluster -> monitoring station just downstream of it.
CLUSTERS = {
    "Kanpur": "1068",
    "Prayagraj": "1049",
    "Varanasi": "1071",
}
BEFORE = (date(2016, 1, 1), date(2019, 12, 31))
AFTER = (date(2023, 1, 1), date(2026, 12, 31))


def write_hindcast(ctx, x) -> dict:
    res = fit.run(ctx, x)
    day_month, starts = month_index(ctx.grid.dates)
    data = {v: monthly_mean(res.conc[v], day_month) for v in VARS}
    data["flow"] = monthly_mean(ctx.forcing.flow, day_month)
    np.savez_compressed(
        HINDCAST,
        months=np.array([m.isoformat() for m in starts]),
        centres=ctx.grid.centres,
        **{v: a.astype(np.float32) for v, a in data.items()},
    )
    return data


def _mean(values, variable):
    if not len(values):
        return None
    return float(np.median(values)) if variable == "fc" else float(np.mean(values))


def write_investment(conn, ctx, x) -> dict:
    """Observed change below each city, and the model with loads held at their 2016 level."""
    actual = fit.run(ctx, x).conc
    frozen = replace(ctx.forcing, year=np.full(len(ctx.grid.dates), 2016.0))
    params, scale = fit.decode(x, ctx.grid)
    frozen.town_scale = scale
    from ganga.model.core import simulate

    without = simulate(frozen, params).conc
    recent = np.array([AFTER[0] <= d <= AFTER[1] for d in ctx.grid.dates])
    clusters = []
    for name, code in CLUSTERS.items():
        row = conn.execute(
            "SELECT name, chainage_km FROM stations WHERE code = %s", (code,)
        ).fetchone()
        if row is None or row[1] is None:
            continue
        cell = ctx.grid.cell(row[1])
        observed, modelled = {}, {}
        for v in ("bod", "fc", "do"):
            vals = {}
            for label, (a, b) in (("before", BEFORE), ("after", AFTER)):
                got = conn.execute(
                    "SELECT value FROM obs_best WHERE station_code = %s AND variable = %s "
                    "AND resolution IN ('month', 'day') AND time BETWEEN %s AND %s",
                    (code, v, a, b),
                ).fetchall()
                vals[label] = [g[0] for g in got]
            observed[v] = {
                "before": _mean(vals["before"], v),
                "after": _mean(vals["after"], v),
                "n_before": len(vals["before"]),
                "n_after": len(vals["after"]),
            }
            agg = np.median if v == "fc" else np.mean
            modelled[v] = {
                "with_investment": float(agg(actual[v][recent, cell])),
                "without_investment": float(agg(without[v][recent, cell])),
            }
        clusters.append(
            {
                "name": name,
                "station": code,
                "station_name": row[0],
                "observed": observed,
                "modelled": modelled,
            }
        )
    best = fit.describe(x)
    out = {
        "note": (
            "Measured columns compare laboratory results just downstream of each city in "
            "2016-19 and 2023-26 (mean BOD, median coliform); they also reflect differences in "
            "river flow between the periods. The model columns are for 2023-26: 'with' is the "
            "calibrated model; 'without' keeps sewage loads at their 2016 level. The 2016 level "
            f"is a single fitted factor ({best['load_2016']:.1f}x for towns, "
            f"{best['diffuse_2016']:.1f}x for diffuse sources), not an account of individual "
            "plants, so treat the split as indicative."
        ),
        "load_2016": best["load_2016"],
        "diffuse_2016": best["diffuse_2016"],
        "clusters": clusters,
    }
    INVESTMENT.write_text(json.dumps(out, indent=1))
    return out


def main(log=print) -> None:
    from ganga.db import connect

    cal = fit.load()
    ctx = build_context()
    write_hindcast(ctx, cal["x"])
    with connect() as conn:
        inv = write_investment(conn, ctx, cal["x"])
    log(
        f"hindcast written for {ctx.grid.dates[0]}..{ctx.grid.dates[-1]}; "
        f"{len(inv['clusters'])} investment clusters"
    )


if __name__ == "__main__":
    main()
