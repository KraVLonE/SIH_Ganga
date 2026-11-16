"""The transport scheme checked against closed-form solutions."""

import time

import numpy as np
import pytest

from ganga.model.core import DAY, Forcing, Params, do_saturation, hydraulics, reaeration, simulate

N_CELLS, DX = 120, 5.0


def forcing(n_days=30, flow=1000.0, upstream=None, **kw):
    shape = (n_days, N_CELLS)
    zeros = np.zeros(shape)
    up = {"bod": 0.0, "nh4": 0.0, "tin": 0.0, "tp": 0.0, "fc": 0.0, "do": 9.09}
    up.update(upstream or {})
    base = dict(
        dx_km=DX,
        flow=np.full(shape, flow),
        air_temp=np.full(shape, 20.0),
        solar=zeros.copy(),
        rain=zeros.copy(),
        inflow=zeros.copy(),
        inflow_conc={s: zeros.copy() for s in ("bod", "nh4", "tin", "tp", "fc", "do")},
        upstream={s: np.full(n_days, v) for s, v in up.items()},
        town_cell=np.array([], dtype=int),
        town_untreated=np.array([]),
        town_treated=np.array([]),
        town_population=np.array([]),
        town_offset_km=np.array([]),
    )
    base.update(kw)
    return Forcing(**base)


# Water temperature equal to air temperature (20 C) and no extras, so rates are the base rates.
PLAIN = Params(tw_a=0.0, tw_b=1.0, sod=0.0, ks=0.0, kfc_light=0.0, kdn=0.0, diffuse_bod=0.0)


def travel_days(p, flow=1000.0):
    u, _ = hydraulics(np.array([flow]), p)
    return (np.arange(N_CELLS) + 1) * DX * 1000.0 / u[0] / DAY


def test_first_order_decay_matches_exponential():
    f = forcing(upstream={"fc": 1e5, "bod": 10.0})
    r = simulate(f, PLAIN)
    t = travel_days(PLAIN)
    np.testing.assert_allclose(r.conc["fc"][-1], 1e5 * np.exp(-PLAIN.kfc * t), rtol=1e-6)
    np.testing.assert_allclose(r.conc["bod"][-1], 10.0 * np.exp(-PLAIN.kd * t), rtol=1e-6)


def test_streeter_phelps_oxygen_sag():
    p = PLAIN.replace(kn=0.0)
    f = forcing(upstream={"bod": 20.0, "do": do_saturation(20.0).item()})
    r = simulate(f, p)
    u, h = hydraulics(np.array([1000.0]), p)
    ka, kd, t = reaeration(u, h)[0], p.kd, travel_days(p)
    deficit = kd * 20.0 / (ka - kd) * (np.exp(-kd * t) - np.exp(-ka * t))
    exact = do_saturation(20.0) - deficit
    got = r.conc["do"][-1]
    assert np.max(np.abs(got - exact)) < 0.03 * deficit.max()  # within 3% of the sag depth
    assert abs(np.argmin(got) - np.argmin(exact)) <= 1  # sag at the right place


def test_pure_dilution_at_a_tributary():
    inflow = np.zeros((30, N_CELLS))
    inflow[:, 40] = 500.0
    flow = np.full((30, N_CELLS), 1000.0)
    flow[:, 40:] = 1500.0
    conc = {s: np.zeros((30, N_CELLS)) for s in ("bod", "nh4", "tin", "tp", "fc", "do")}
    conc["tp"][:, 40] = 0.4
    p = PLAIN.replace(kp=0.0)
    r = simulate(forcing(flow=flow, inflow=inflow, inflow_conc=conc, upstream={"tp": 1.0}), p)
    want = (1000.0 * 1.0 + 500.0 * 0.4) / 1500.0
    np.testing.assert_allclose(r.conc["tp"][-1, :40], 1.0, rtol=1e-9)
    np.testing.assert_allclose(r.conc["tp"][-1, 40:], want, rtol=1e-9)


def test_conservative_tracer_mass_balance():
    """Everything that enters leaves: boundary + town load = outflow, to within 0.1%."""
    p = PLAIN.replace(kp=0.0, raw_tp=5.0, delivery_km=1e9)
    f = forcing(
        n_days=60,
        upstream={"tp": 0.2},
        town_cell=np.array([30]),
        town_untreated=np.array([200.0]),  # MLD
        town_treated=np.array([0.0]),
        town_population=np.array([10.0]),
        town_offset_km=np.array([0.0]),
    )
    r = simulate(f, p)
    mass_in = 1000.0 * 0.2 + 200.0 * 1000.0 / DAY * 5.0  # g/s
    mass_out = r.conc["tp"][-1, -1] * 1000.0
    assert mass_out == pytest.approx(mass_in, rel=1e-3)


def test_pulse_travels_at_the_water_speed():
    """A one-day spill should peak downstream after the travel time, not instantly."""
    n_days = 40
    extra = np.zeros((n_days, N_CELLS))
    extra[5, 10] = 1e6
    p = PLAIN.replace(kp=0.0)
    r = simulate(forcing(n_days=n_days, extra={"tp": extra}), p, spinup_days=0)
    t = travel_days(p)
    for cell in (40, 80, 110):
        peak_day = int(np.argmax(r.conc["tp"][:, cell]))
        expected = 5 + (t[cell] - t[9])
        assert abs(peak_day - expected) <= 1.0
    assert r.conc["tp"][5, 110] == 0.0  # nothing far downstream on the day of the spill


def test_nitrification_conserves_nitrogen_and_uses_oxygen():
    p = PLAIN.replace(kd=0.0)
    f = forcing(upstream={"nh4": 5.0, "tin": 6.0, "do": do_saturation(20.0).item()})
    r = simulate(f, p)
    np.testing.assert_allclose(r.conc["nh4"][-1] + r.conc["no3"][-1], 6.0, rtol=1e-6)
    assert r.conc["nh4"][-1, -1] < 1.0 and r.conc["no3"][-1, -1] > 5.0
    assert r.conc["do"][-1].min() < do_saturation(20.0) - 1.0


def test_storm_raises_load_and_sunlight_kills_coliform():
    town = dict(
        town_cell=np.array([20]),
        town_untreated=np.array([10.0]),
        town_treated=np.array([200.0]),
        town_population=np.array([20.0]),
        town_offset_km=np.array([0.0]),
    )
    dry = simulate(forcing(**town), PLAIN)
    rain = np.zeros((30, N_CELLS))
    rain[:, :] = 40.0
    wet = simulate(forcing(rain=rain, **town), PLAIN)
    assert wet.conc["bod"][-1, 25] > 1.5 * dry.conc["bod"][-1, 25]
    sunny = simulate(
        forcing(solar=np.full((30, N_CELLS), 20.0), **town), PLAIN.replace(kfc_light=0.05)
    )
    assert sunny.conc["fc"][-1, 60] < 0.8 * dry.conc["fc"][-1, 60]


def test_ten_years_runs_in_seconds():
    rng = np.random.default_rng(0)
    n_days, shape = 3650, (3650, 274)
    f = forcing(n_days=n_days)
    f.flow = 800.0 + 400.0 * rng.random(shape).cumsum(axis=1) / 274
    for name in ("air_temp", "solar", "rain", "inflow"):
        setattr(f, name, np.zeros(shape) + (20.0 if name == "air_temp" else 0.0))
    f.inflow_conc = {s: np.zeros(shape) for s in f.inflow_conc}
    start = time.perf_counter()
    simulate(f, Params())
    assert time.perf_counter() - start < 20.0


def test_diffuse_load_reaches_equilibrium_and_algae_add_oxygen():
    """Uniform load w per km with decay k settles at w x u / (k x Q) far downstream."""
    p = PLAIN.replace(diffuse_bod=200.0, kd=0.5, kn=0.0)
    r = simulate(forcing(n_days=60), p)
    u, _ = hydraulics(np.array([1000.0]), p)
    km_per_day = u[0] * DAY / 1000.0
    want = 200.0 * 1000.0 / DAY * km_per_day / 0.5 / 1000.0  # g/s per km x km / (1/day) / Q
    t = travel_days(p)[-1]
    assert r.conc["bod"][-1, -1] == pytest.approx(want * (1 - np.exp(-0.5 * t)), rel=0.02)
    sunny = simulate(forcing(n_days=60, solar=np.full((60, N_CELLS), 20.0)), p.replace(photo=0.1))
    assert sunny.conc["do"][-1, -1] > r.conc["do"][-1, -1] + 0.3


def test_log_form_matches_direct_form_and_survives_extreme_decay():
    from ganga.model import core

    f = forcing(
        upstream={"fc": 1e6},
        town_cell=np.array([60]),
        town_untreated=np.array([50.0]),
        town_treated=np.array([0.0]),
        town_population=np.array([5.0]),
        town_offset_km=np.array([0.0]),
    )
    direct = simulate(f, PLAIN).conc["fc"][-1]
    old = core.EXP_SAFE
    core.EXP_SAFE = -1.0  # force the logarithmic path
    try:
        logged = simulate(f, PLAIN).conc["fc"][-1]
    finally:
        core.EXP_SAFE = old
    np.testing.assert_allclose(logged, direct, rtol=1e-6)
    extreme = simulate(forcing(flow=20.0, upstream={"fc": 1e6}), PLAIN.replace(kfc=40.0))
    assert np.isfinite(extreme.conc["fc"]).all() and extreme.conc["fc"][-1, -1] < 1e-3


def test_load_trend_scales_town_loads_over_the_years():
    town = dict(
        town_cell=np.array([20]),
        town_untreated=np.array([100.0]),
        town_treated=np.array([0.0]),
        town_population=np.array([10.0]),
        town_offset_km=np.array([0.0]),
    )
    f = forcing(**town)
    f.year = np.full(30, 2016.0)
    early = simulate(f, PLAIN.replace(load_2016=3.0)).conc["bod"][-1, 25]
    f.year = np.full(30, 2025.0)
    late = simulate(f, PLAIN.replace(load_2016=3.0)).conc["bod"][-1, 25]
    assert early == pytest.approx(3.0 * late, rel=1e-6)
