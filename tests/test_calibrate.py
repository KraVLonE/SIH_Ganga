from datetime import date, timedelta

import numpy as np

from ganga.calibrate import evaluate as ev
from ganga.calibrate import fit
from ganga.calibrate.data import Targets, month_index, monthly_mean
from ganga.model.setup import Grid


def _targets(n=120, seed=0):
    rng = np.random.default_rng(seed)
    when = np.array([date(2020, 1, 1) + timedelta(days=30 * (i % 60)) for i in range(n)])
    station = np.array([f"S{i % 4}" for i in range(n)])
    held = station == "S3"
    return Targets(
        variable=np.arange(n) % 3,
        cell=np.array([int(s[1]) * 10 for s in station]),
        month=np.arange(n) % 60,
        day=np.full(n, -1),
        value=rng.normal(5, 1, n),
        station=station,
        when=when,
        train=(when < date(2023, 1, 1)) & ~held,
        holdout_station=held,
    )


def test_month_index_and_means():
    dates = [date(2024, 1, 30) + timedelta(days=i) for i in range(5)]  # 30, 31 Jan; 1-3 Feb
    idx, starts = month_index(dates)
    assert list(idx) == [0, 0, 1, 1, 1] and starts == [date(2024, 1, 1), date(2024, 2, 1)]
    vals = np.array([[1.0], [3.0], [10.0], [20.0], [30.0]])
    np.testing.assert_allclose(monthly_mean(vals, idx), [[2.0], [20.0]])


def test_decode_respects_bounds_logs_and_town_groups():
    grid = Grid(
        5.0, np.arange(3) * 5.0, [date(2024, 1, 1)], {}, ["Kanpur Nagar", "Ballia", "Varanasi"]
    )
    b = np.array(fit.bounds())
    params, scale = fit.decode(b[:, 0], grid)
    spec = {name: (lo, hi, log) for name, lo, hi, log in fit.SPEC}
    assert params.kd == spec["kd"][0]
    assert params.raw_fc == np.float64(spec["raw_fc"][0]) or abs(params.raw_fc - 1e6) < 1
    assert scale[1] == 1.0  # a town outside every group keeps the global multiplier
    assert scale[0] == np.float64(fit.GROUP_BOUNDS[0]) or abs(scale[0] - 0.2) < 1e-9
    named = fit.describe(b[:, 1])
    assert abs(named["town_scale_varanasi"] - fit.GROUP_BOUNDS[1]) < 1e-9
    assert set(spec) <= set(fit.params_fields())  # every calibrated name is a real parameter


def test_behavioural_sets_are_distinct_and_best_first():
    xs = np.array([[0.0, 1.0], [0.0, 1.0], [0.5, 1.0], [2.0, 2.0], [3.0, 3.0]])
    fit_result = {"xs": xs, "scores": np.array([1.0, 1.0, 1.02, 1.5, 9.0])}
    sets = fit.behavioural(fit_result, tolerance=0.05, min_sets=2, max_sets=10)
    assert sets.tolist() == [[0.0, 1.0], [0.5, 1.0]]
    assert len(fit.behavioural(fit_result, tolerance=0.05, min_sets=3, max_sets=10)) == 3


def test_metrics_perfect_and_biased():
    obs = np.array([1.0, 2.0, 3.0, 4.0])
    perfect = ev.metrics(obs, obs)
    assert perfect["rmse"] == 0 and perfect["nse"] == 1 and abs(perfect["kge"] - 1) < 1e-12
    off = ev.metrics(obs + 1, obs)
    assert off["bias"] == 1 and off["rmse"] == 1 and off["nse"] < 1
    assert ev.metrics(obs[:2], obs[:2]) == {"n": 2}  # too few to score


def test_climatology_baseline_uses_training_data_only():
    t = _targets()
    base = ev.climatology_baseline(t, t.cell)
    assert np.isfinite(base).all()
    # Changing a validation value must not change any prediction.
    t2 = _targets()
    i = int(np.flatnonzero(~t2.train)[0])
    t2.value[i] += 100
    np.testing.assert_allclose(ev.climatology_baseline(t2, t2.cell), base)
    # Unseen stations borrow from the nearest training station (S2 is nearest to S3).
    s3, s2 = (
        (t.station == "S3") & (t.variable == 0),
        t.train & (t.station == "S2") & (t.variable == 0),
    )
    assert abs(base[s3].mean() - t.value[s2].mean()) < 1.0


def test_interval_coverage_is_about_right_for_well_specified_noise():
    rng = np.random.default_rng(1)
    t = _targets(n=3000, seed=2)
    truth = np.full(len(t), 5.0)
    t.value = truth + rng.normal(0, 1.0, len(t))
    preds = np.stack([truth, truth])
    cover = ev.interval_coverage(preds, np.array([1.0, 1.0, 1.0]), t, np.ones(len(t), bool))
    for v in ("do", "bod", "fc"):
        assert 0.86 < cover[v] < 0.94
    narrow = ev.interval_coverage(preds, np.array([0.2, 0.2, 0.2]), t, np.ones(len(t), bool))
    assert narrow["do"] < 0.5  # an overconfident band is exposed
