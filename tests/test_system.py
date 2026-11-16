"""End-to-end checks against the local database (skipped when it is not populated)."""

from datetime import timedelta

import numpy as np
import psycopg
import pytest

from ganga.calibrate import fit
from ganga.db import connect

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def conn():
    try:
        c = connect(connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database container not running")
    n = c.execute("SELECT count(*) FROM timeseries WHERE variable = 'flow'").fetchone()[0]
    if n == 0:
        c.close()
        pytest.skip("no driver data loaded")
    yield c
    c.close()


@pytest.fixture(scope="module")
def today(conn):
    last = conn.execute(
        "SELECT max(time) FROM timeseries WHERE variable = 'flow' AND member = 0"
    ).fetchone()[0]
    return last - timedelta(days=6)


@pytest.fixture(scope="module")
def calibration():
    """Mid-range parameters: enough to exercise the machinery without the fitted file."""
    b = np.array(fit.bounds())
    mid = b.mean(axis=1)
    rng = np.random.default_rng(0)
    sets = np.array(
        [
            np.clip(mid + rng.normal(0, 0.03, mid.size) * np.ptp(b, axis=1), b[:, 0], b[:, 1])
            for _ in range(4)
        ]
    )
    return {
        "x": mid,
        "behavioural": sets,
        "created": "test",
        "score": 0.0,
        "residual_sigma": {"do": 0.7, "bod": 0.8, "fc": 0.35},
    }


def _profiles(result, variable):
    return np.array(result["baseline"][variable]), np.array(result["scenario"][variable])


def test_scenarios_move_in_the_expected_direction(conn, today, calibration):
    from ganga import scenario

    r = scenario.run(conn, today, "plant_offline", calibration=calibration, town="Kanpur Nagar")
    km = np.array(r["chainage_km"])
    below = (km > 600) & (km < 700)
    base, alt = _profiles(r, "bod")
    assert (alt[-1, below] > base[-1, below]).all()
    assert np.allclose(alt[-1, km < 560], base[-1, km < 560])  # nothing changes upstream
    base, alt = _profiles(r, "do")
    assert alt[-1, below].min() <= base[-1, below].min()

    r = scenario.run(conn, today, "load_change", calibration=calibration, percent=-50)
    base, alt = _profiles(r, "fc")
    assert (alt[-1] <= base[-1] + 1e-6).all() and alt[-1].sum() < base[-1].sum()

    r = scenario.run(
        conn, today, "rain", calibration=calibration, town="Varanasi", rain_mm=80, day_offset=1
    )
    base, alt = _profiles(r, "fc")
    assert alt[1:].max() > base[1:].max()
    assert np.allclose(alt[0], base[0])  # the day before the rain is unchanged

    r = scenario.run(conn, today, "low_flow", calibration=calibration, percent=-40)
    base, alt = _profiles(r, "bod")
    assert alt[-1].mean() > base[-1].mean()


def test_spill_moves_downstream_over_days(conn, today, calibration):
    from ganga import scenario

    r = scenario.run(
        conn, today, "spill", calibration=calibration, chainage_km=600, tonnes_bod=500, day_offset=0
    )
    base, alt = _profiles(r, "bod")
    km = np.array(r["chainage_km"])
    extra = alt - base
    peaks = [km[int(np.argmax(day))] for day in extra]
    assert peaks[0] >= 595 and all(b >= a for a, b in zip(peaks, peaks[1:], strict=False))
    assert peaks[-1] > peaks[0] + 50  # it has travelled
    arrivals = {p["name"]: p["arrival_days"] for p in r["places"]}
    assert arrivals["Haridwar"] is None and arrivals["Dalmau"] > 0


def test_bad_scenario_inputs_are_rejected(conn, today, calibration):
    from ganga import scenario

    for kw in (
        dict(kind="plant_offline", town="Atlantis"),
        dict(kind="spill", chainage_km=9999, tonnes_bod=10),
        dict(kind="rain", town="Varanasi", rain_mm=-5),
        dict(kind="teleport"),
    ):
        with pytest.raises(scenario.ScenarioError):
            scenario.run(conn, today, kw.pop("kind"), calibration=calibration, **kw)


def test_seeded_spill_raises_a_pulse_alert_with_arrival_times(conn, today, calibration):
    from ganga.alerts.rules import evaluate
    from ganga.forecast.run import HORIZON_DAYS, SPINUP_DAYS, run_ensemble, summarise
    from ganga.model.core import DAY

    n_days = SPINUP_DAYS + HORIZON_DAYS + 1
    quiet = run_ensemble(conn, today, calibration)
    n_cells = quiet.flow.shape[2]
    spill = np.zeros((n_days, n_cells))
    spill[SPINUP_DAYS + 1, quiet.grid.cell(600)] = 3000 * 1e6 / DAY  # 3000 t BOD tomorrow
    ens = run_ensemble(conn, today, calibration, extra={"bod": spill})
    alerts = evaluate(conn, today, ens, summarise(ens))
    pulses = [a for a in alerts if a.kind == "pulse" and a.details["variable"] == "bod"]
    assert pulses, [a.message for a in alerts]
    p = pulses[0]
    assert 590 <= p.chainage_km <= 700 and p.severity == "severe"
    arrivals = p.details["arrivals"]
    assert arrivals and all(a["days"] > 0 for a in arrivals)
    assert [a["days"] for a in arrivals] == sorted(a["days"] for a in arrivals)
    quiet_pulses = [
        a
        for a in evaluate(conn, today, quiet, summarise(quiet))
        if a.kind == "pulse" and a.details["variable"] == "bod" and 590 <= a.chainage_km <= 700
    ]
    assert not quiet_pulses  # no spill, no pulse there


def test_ensemble_bands_are_ordered_and_probabilities_valid(conn, today, calibration):
    from ganga.forecast.run import run_ensemble, summarise

    s = summarise(run_ensemble(conn, today, calibration))
    for v in ("do", "bod", "fc", "flow"):
        assert (s[v]["p05"] <= s[v]["p95"] + 1e-9).all()
        assert np.isfinite(s[v]["p50"]).all() and (s[v]["p50"] >= 0).all()
    p = s["p_unfit"]["p50"]
    assert ((p >= 0) & (p <= 1)).all()
    assert (p >= s["p_fc"]["p50"] - 1e-9).all()  # failing any limit includes failing coliform


def test_api_endpoints(conn):
    from fastapi.testclient import TestClient

    from ganga.api.app import app

    if conn.execute("SELECT count(*) FROM forecast_runs").fetchone()[0] == 0:
        pytest.skip("no forecast issued yet")
    client = TestClient(app)
    meta = client.get("/api/meta").json()
    assert meta["first_day"] < meta["issued_on"] < meta["last_day"]
    nodes = client.get("/api/nodes").json()
    assert nodes[0]["slug"] == "haridwar" and nodes[-1]["slug"] == "patna"
    f = client.get("/api/forecast", params={"node": "varanasi_us"}).json()
    assert len(f["series"]["fc"]) == 16 and len(f["rain"]) >= 10
    assert sum(p["forecast"] for p in f["series"]["do"]) == 5
    assert all(p["p05"] <= p["p50"] * 1.5 + 1 for p in f["series"]["bod"])
    prof = client.get("/api/profile", params={"variable": "fc"}).json()
    river = client.get("/api/river").json()
    assert len(prof["p50"]) == len(river["features"]) == len(prof["chainage_km"])
    assert client.get("/api/forecast", params={"chainage": 99999}).status_code == 422
    assert client.get("/api/forecast", params={"node": "nowhere"}).status_code == 404
    assert client.get("/api/profile", params={"variable": "gold"}).status_code == 422
    obs = client.get("/api/observations", params={"chainage": 1047, "variable": "fc"}).json()
    assert obs and {"station", "date", "value", "source_file"} <= set(obs[0])
    assert isinstance(client.get("/api/alerts").json(), list)
    assert len(client.get("/api/loads").json()) >= 25
    bad = client.post("/api/scenario", json={"kind": "plant_offline", "town": "Atlantis"})
    assert bad.status_code == 422


def test_assistant_numbers_match_the_api(conn):
    """The built-in responder must quote exactly what the forecast endpoint reports."""
    from fastapi.testclient import TestClient

    from ganga import assistant
    from ganga.api.app import app

    if conn.execute("SELECT count(*) FROM forecast_runs").fetchone()[0] == 0:
        pytest.skip("no forecast issued yet")
    client = TestClient(app)
    f = client.get(
        "/api/forecast", params={"node": "varanasi_us", "variables": "bod,p_unfit"}
    ).json()
    today = f["issued_on"]
    bod = next(p["p50"] for p in f["series"]["bod"] if p["date"] == today)
    risk = next(p["p50"] for p in f["series"]["p_unfit"] if p["date"] == today)
    out = assistant.ask_builtin("Is it safe to bathe at Varanasi today?")
    assert f"BOD {bod:.1f} mg/L" in out["answer"]
    assert f"{risk:.0%} chance of failing" in out["answer"]
    assert out["used"] == ["get_forecast"]
    worst = assistant.ask_builtin("Where is the river worst today?")
    assert worst["used"] == ["worst_places"] and "model estimates" in worst["answer"]
    assert "Places:" in assistant.ask_builtin("What is the meaning of life?")["answer"]
