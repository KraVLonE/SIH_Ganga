from datetime import date

import psycopg
import pytest

from ganga.db import connect
from ganga.ingest.cpcb_wqi import parse_snapshot
from ganga.ingest.drivers import FLOW_RENAME, WEATHER_RENAME, _year_chunks, upsert
from ganga.ingest.openmeteo import parse_daily

WQI_SNIPPET = """
marker610 = L.marker(
    [25.503263, 81.854428],
    { icon: L.icon({ iconUrl: imageDefault }) }
).addTo(map);
var contentStringDefault0 = '<div id="content">' +
    '<div id="siteNotice">' +
    '<p><h5 class="head">1046-RIVER GANGA AT PRAYAGRAJ (RASOOLABAD), U.P.</h5></p>' +
    '<p><font color="blue"><h6>Sampling Month:Oct-2026</h6></font></p>' +
    '<p>Dissolved Oxygen: 8.1</p>' +
    '<p>Bio Chemical Oxygen Demand: 2.4</p>' +
    '<p>Total Coliform: 2100</p>' +
    '<p>pH: 7.68</p></div></div>';
marker610.bindPopup(contentStringDefault0);
marker610 = L.marker([25.406871, 81.904923], {}).addTo(map);
var contentStringDefault1 = '<h5 class="head">1049-RIVER YAMUNA TEST, U.P.</h5>' +
    '<p>Dissolved Oxygen: 7.0</p>';
"""


def test_parse_wqi_snapshot():
    rows = parse_snapshot(WQI_SNIPPET)
    assert [r["code"] for r in rows] == ["1046", "1049"]
    a = rows[0]
    assert (a["lat"], a["lon"]) == (25.503263, 81.854428)
    assert a["name"].startswith("RIVER GANGA AT PRAYAGRAJ")
    assert a["month"] == date(2026, 10, 1)
    assert a["values"] == {"do": 8.1, "bod": 2.4, "tc": 2100.0, "ph": 7.68}
    assert a["river"] == "Ganga" and rows[1]["river"] is None
    # Values must not leak from one station's popup into the next.
    assert rows[1]["values"] == {"do": 7.0} and rows[1]["month"] is None


def test_parse_daily_flow_with_ensemble_and_gaps():
    payload = [
        {
            "daily": {
                "time": ["2026-11-15", "2026-11-16"],
                "river_discharge": [100.0, None],
                "river_discharge_member01": [90.0, 95.0],
                "river_discharge_member50": [110.0, 120.0],
                "river_discharge_max": [1.0, 1.0],
            }
        },
        {"daily": {"time": ["2026-11-15"], "river_discharge": [5.0]}},
    ]
    rows = list(parse_daily(payload, FLOW_RENAME))
    assert (0, "flow", 0, date(2026, 11, 15), 100.0) in rows
    assert (0, "flow", 50, date(2026, 11, 16), 120.0) in rows
    assert (1, "flow", 0, date(2026, 11, 15), 5.0) in rows
    assert len(rows) == 6  # the None and the unrequested field are dropped


def test_parse_daily_weather_renames_fields():
    payload = [
        {
            "daily": {
                "time": ["2026-11-15"],
                "precipitation_sum": [3.2],
                "temperature_2m_mean": [21.0],
                "shortwave_radiation_sum": [14.5],
            }
        }
    ]
    got = {(v, val) for _, v, _, _, val in parse_daily(payload, WEATHER_RENAME)}
    assert got == {("rain", 3.2), ("air_temp", 21.0), ("solar", 14.5)}


def test_year_chunks_cover_range_without_overlap():
    chunks = list(_year_chunks(date(2016, 3, 1), date(2018, 2, 10)))
    assert chunks == [
        (date(2016, 3, 1), date(2016, 12, 31)),
        (date(2017, 1, 1), date(2017, 12, 31)),
        (date(2018, 1, 1), date(2018, 2, 10)),
    ]


@pytest.mark.db
def test_upsert_is_idempotent():
    try:
        conn = connect(connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database container not running")
    with conn:
        row = conn.execute("SELECT id FROM nodes ORDER BY chainage_km LIMIT 1").fetchone()
        if row is None:
            pytest.skip("network not built")
        nid = row[0]
        day = date(1900, 1, 1)  # far outside real data

        def count():
            return conn.execute(
                "SELECT count(*), max(value) FROM timeseries WHERE source = 'test'"
            ).fetchone()

        try:
            upsert(conn, [nid], "test", [(0, "flow", 0, day, 1.0)])
            upsert(conn, [nid], "test", [(0, "flow", 0, day, 2.0)])
            assert count() == (1, 2.0)  # second write replaced, did not duplicate
        finally:
            conn.execute("DELETE FROM timeseries WHERE source = 'test'")
            conn.commit()
