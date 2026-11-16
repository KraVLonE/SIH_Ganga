"""Observation targets and tributary boundary quality for calibration."""

from dataclasses import dataclass
from datetime import date

import numpy as np

from ganga.model.setup import TRIBUTARY, Grid

# Monitoring station nearest each tributary's mouth (CPCB station codes).
TRIBUTARY_STATION = {
    "ramganga": "1064",  # Ramganga at Kannauj, before confluence
    "kali": "1065",  # Kali near Gumatiya, Kannauj, before confluence
    "yamuna": "1069",  # Yamuna at Prayagraj d/s (Balua Ghat)
    "gomti": "1072",  # Gomti at Rajwari, Varanasi
    "ghaghara": "1355",  # Ghaghara at Turtipar, Deoria
}
VARIABLES = ("do", "bod", "fc")
HOLDOUT_EVERY = 5  # every fifth main-stem station is kept out of calibration
SPLIT = date(2023, 1, 1)  # calibrate before, validate from this date


@dataclass
class Targets:
    variable: np.ndarray  # index into VARIABLES
    cell: np.ndarray
    month: np.ndarray  # index of the calendar month within the run, or -1 for dated samples
    day: np.ndarray  # day index for dated samples, else -1
    value: np.ndarray  # observed; log10 for coliform
    station: np.ndarray  # station code
    when: np.ndarray  # date of the observation (month start for monthly values)
    train: np.ndarray  # True if used for calibration
    holdout_station: np.ndarray  # True if the station is never used for calibration

    def __len__(self) -> int:
        return len(self.value)


def month_index(dates: list[date]) -> tuple[np.ndarray, list[date]]:
    """For each day, the index of its month; and the list of month starts."""
    starts, idx = [], np.empty(len(dates), dtype=int)
    for i, d in enumerate(dates):
        m = d.replace(day=1)
        if not starts or starts[-1] != m:
            starts.append(m)
        idx[i] = len(starts) - 1
    return idx, starts


def monthly_mean(values: np.ndarray, day_month: np.ndarray) -> np.ndarray:
    """[day, cell] -> [month, cell] means."""
    n_months = day_month[-1] + 1
    sums = np.zeros((n_months, values.shape[1]))
    np.add.at(sums, day_month, values)
    return sums / np.bincount(day_month, minlength=n_months)[:, None]


def load_targets(conn, grid: Grid) -> Targets:
    rows = conn.execute(
        """
        SELECT o.station_code, s.chainage_km, o.variable, o.time, o.resolution, o.value
        FROM obs_best o JOIN stations s ON s.code = o.station_code
        WHERE s.chainage_km IS NOT NULL AND o.variable = ANY(%s)
          AND o.resolution IN ('month', 'day') AND o.time BETWEEN %s AND %s
        ORDER BY s.chainage_km, o.station_code, o.time
        """,
        (list(VARIABLES), grid.dates[0], grid.dates[-1]),
    ).fetchall()
    stations = sorted({(r[1], r[0]) for r in rows})
    holdout = {code for i, (_, code) in enumerate(stations) if i % HOLDOUT_EVERY == 2}
    _, starts = month_index(grid.dates)
    month_of = {m: i for i, m in enumerate(starts)}
    var, cell, month, day, value, station, when = [], [], [], [], [], [], []
    for code, chainage, variable, t, resolution, v in rows:
        if variable == "fc":
            if v <= 0:
                continue
            v = float(np.log10(v))
        elif variable == "bod" and v > 60:
            continue  # not river water
        var.append(VARIABLES.index(variable))
        cell.append(grid.cell(chainage))
        if resolution == "day":
            month.append(-1)
            day.append(grid.day(t))
        else:
            month.append(month_of[t.replace(day=1)])
            day.append(-1)
        value.append(v)
        station.append(code)
        when.append(t)
    station = np.array(station)
    when = np.array(when)
    held = np.isin(station, list(holdout))
    return Targets(
        np.array(var),
        np.array(cell),
        np.array(month),
        np.array(day),
        np.array(value, float),
        station,
        when,
        (when < SPLIT) & ~held,
        held,
    )


def predict(conc: dict[str, np.ndarray], targets: Targets, day_month: np.ndarray) -> np.ndarray:
    """Model value matching each target (monthly mean or that day; log10 for coliform)."""
    out = np.empty(len(targets))
    for vi, name in enumerate(VARIABLES):
        monthly = monthly_mean(conc[name], day_month)
        sel = targets.variable == vi
        m = sel & (targets.month >= 0)
        d = sel & (targets.day >= 0)
        out[m] = monthly[targets.month[m], targets.cell[m]]
        out[d] = conc[name][targets.day[d], targets.cell[d]]
        if name == "fc":
            out[sel] = np.log10(np.maximum(out[sel], 1.0))
    return out


def tributary_quality(conn) -> dict[str, dict]:
    """Observed quality at each tributary mouth as a 12-month climatology (median by month).

    BOD, DO and coliform come from the station. Ammonia, nitrogen and phosphorus are not
    monitored monthly, so the defaults are scaled by observed BOD relative to the default BOD.
    """
    out = {}
    for name, code in TRIBUTARY_STATION.items():
        rows = conn.execute(
            """
            SELECT variable, extract(month FROM time)::int,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY value)
            FROM obs_best WHERE station_code = %s AND resolution = 'month'
              AND variable = ANY(%s) GROUP BY 1, 2
            """,
            (code, list(VARIABLES)),
        ).fetchall()
        clim = {v: np.full(12, np.nan) for v in VARIABLES}
        for variable, m, med in rows:
            clim[variable][m - 1] = med
        base = dict(TRIBUTARY[name])
        quality = {}
        for v in VARIABLES:
            c = clim[v]
            if np.isnan(c).all():
                quality[v] = base[v]
                continue
            c[np.isnan(c)] = np.nanmedian(c)
            quality[v] = c
        ratio = np.asarray(quality["bod"]) / base["bod"]
        for v in ("nh4", "tin", "tp"):
            quality[v] = base[v] * ratio
        out[name] = quality
    return out
