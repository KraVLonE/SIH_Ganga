"""Skill metrics, baselines and uncertainty calibration."""

import numpy as np

from ganga.calibrate.data import VARIABLES, Targets


def metrics(pred: np.ndarray, obs: np.ndarray) -> dict[str, float]:
    if len(obs) < 3:
        return {"n": int(len(obs))}
    err = pred - obs
    var = float(np.var(obs))
    out = {
        "n": int(len(obs)),
        "bias": float(err.mean()),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "mae": float(np.mean(np.abs(err))),
        "nse": float(1.0 - np.mean(err**2) / var) if var > 0 else float("nan"),
    }
    if var > 0 and np.std(pred) > 0:
        r = float(np.corrcoef(pred, obs)[0, 1])
        alpha = float(np.std(pred) / np.std(obs))
        beta = float(np.mean(pred) / np.mean(obs)) if np.mean(obs) != 0 else float("nan")
        out["r"] = r
        out["kge"] = float(1.0 - np.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2))
    return out


def splits(t: Targets) -> dict[str, np.ndarray]:
    return {
        "train": t.train,
        "later_years": ~t.train & ~t.holdout_station,  # same stations, unseen years
        "unseen_stations": t.holdout_station,  # stations never used
    }


def skill_table(pred: np.ndarray, t: Targets) -> dict:
    out = {}
    for name, mask in splits(t).items():
        out[name] = {
            v: metrics(pred[mask & (t.variable == i)], t.value[mask & (t.variable == i)])
            for i, v in enumerate(VARIABLES)
        }
    return out


def climatology_baseline(t: Targets, cells: np.ndarray) -> np.ndarray:
    """Prediction from observed climatology alone (no model).

    For stations with training data: that station's mean for the calendar month over the
    training years. For unseen stations: the same from the nearest training station.
    """
    months = np.array([d.month for d in t.when])
    pred = np.full(len(t), np.nan)
    for i in range(len(VARIABLES)):
        tr = t.train & (t.variable == i)
        table: dict[tuple[str, int], float] = {}
        overall: dict[str, float] = {}
        for code in np.unique(t.station[tr]):
            s = tr & (t.station == code)
            overall[code] = float(t.value[s].mean())
            for m in range(1, 13):
                sm = s & (months == m)
                if sm.any():
                    table[(code, m)] = float(t.value[sm].mean())
        train_cells = {code: cells[t.station == code][0] for code in overall}
        for code in np.unique(t.station[t.variable == i]):
            sel = (t.variable == i) & (t.station == code)
            src = code
            if code not in overall:
                here = cells[sel][0]
                src = min(train_cells, key=lambda c: abs(train_cells[c] - here))
            for m in range(1, 13):
                sm = sel & (months == m)
                if sm.any():
                    pred[sm] = table.get((src, m), overall[src])
    return pred


def residual_sigma(pred: np.ndarray, t: Targets) -> np.ndarray:
    """Spread of calibration residuals per variable (model + observation noise).

    Uses the median absolute deviation, so a few gross outliers do not inflate the band.
    """
    out = []
    for i in range(len(VARIABLES)):
        sel = t.train & (t.variable == i)
        err = pred[sel] - t.value[sel]
        out.append(1.4826 * np.median(np.abs(err - np.median(err))))
    return np.array(out)


def interval_coverage(
    preds: np.ndarray,
    sigma: np.ndarray,
    t: Targets,
    mask: np.ndarray,
    level: float = 0.90,
    seed: int = 0,
) -> dict[str, float]:
    """Share of observations inside the central `level` band.

    preds is [parameter set, target]. The band combines parameter spread with residual noise.
    """
    rng = np.random.default_rng(seed)
    lo_q, hi_q = (1 - level) / 2, 1 - (1 - level) / 2
    out = {}
    for i, v in enumerate(VARIABLES):
        sel = mask & (t.variable == i)
        if sel.sum() < 3:
            continue
        # Enough draws that the 5% and 95% points are well estimated even with few sets.
        per_set = max(20, -(-600 // preds.shape[0]))
        draws = preds[:, sel][:, None, :] + rng.normal(
            0, sigma[i], (preds.shape[0], per_set, sel.sum())
        )
        draws = draws.reshape(-1, sel.sum())
        lo, hi = np.quantile(draws, [lo_q, hi_q], axis=0)
        obs = t.value[sel]
        out[v] = float(np.mean((obs >= lo) & (obs <= hi)))
        out[v + "_width"] = float(np.mean(hi - lo))
    return out
