from datetime import date

import numpy as np

from ganga.ingest.cpcb_interstate import parse_value
from ganga.ingest.loads import parse_up_table
from ganga.ingest.rowtable import Token, extract_rows
from ganga.ingest.sentinel import best_per_month, indices
from ganga.ingest.uppcb import (
    Report,
    _fix_decimals,
    _to_values,
    decide_month,
    month_from_name,
    month_from_text,
    upload_date,
)

CODES = {"10150", "1062", "2488", "1300"}


def _line(y, code, *vals, xs=(100, 400, 500, 600, 700)):
    toks = [Token(code, xs[0], y, 20)]
    toks += [Token(v, x, y, 20) for v, x in zip(vals, xs[1:], strict=False) if v is not None]
    return toks


def test_rows_assign_values_by_column_and_keep_gaps():
    tokens = (
        _line(100, "10150", "9.40", "1.1", "940", "400")
        + _line(140, "1062", "8.30", "1.7", None, None)  # coliform not measured
        + _line(180, "2488", "7.10", "2.2", "1300", "610")  # 1300 is also a station code
        + [Token("Bijnor", 250, 100, 20), Token("29.37347", 300, 100, 20)]  # other columns
    )
    rows = {r.code: r.values for r in extract_rows(tokens, CODES)}
    assert rows["10150"] == [9.4, 1.1, 940.0, 400.0]
    assert rows["1062"] == [8.3, 1.7, None, None]
    assert rows["2488"] == [7.1, 2.2, 1300.0, 610.0]
    assert "1300" not in rows  # a value in a data column is not a station


def test_missed_code_is_reread():
    tokens = (
        _line(100, "10150", "9.40", "1.1", "940", "400")
        + _line(140, "1062", "8.30", "1.7", "500", "300")
        + [
            Token(v, x, 180, 20)
            for v, x in zip(("7.1", "2.2", "800", "610"), (400, 500, 600, 700), strict=True)
        ]
    )
    rows = extract_rows(
        tokens, CODES, read_code=lambda x, y, h: "2488" if abs(y - 180) < 5 else None
    )
    assert {r.code for r in rows} == {"10150", "1062", "2488"}
    assert next(r for r in rows if r.code == "2488").values == [7.1, 2.2, 800.0, 610.0]


def test_decimal_repair_and_value_checks():
    rows = extract_rows(
        _line(100, "10150", "1050", "25", "940", "400")
        + _line(140, "1062", "10.40", "2.7", "900", "500")
        + _line(180, "2488", "9.90", "2.1", "300", "700"),
        CODES,
    )
    _fix_decimals(rows)
    by = {r.code: _to_values(r) for r in rows}
    assert by["10150"]["do"] == 10.5  # '1050' had lost its decimal point
    assert by["10150"]["bod"] == 25.0  # plausible as printed, left alone
    assert "fc" not in by["2488"] and "tc" not in by["2488"]  # fecal > total: misread, dropped
    assert by["2488"]["do"] == 9.9


def test_month_from_file_names():
    cases = {
        "wb_11012018river-dec2017.pdf": date(2017, 12, 1),
        "wb_NWMP-Dec_170123.pdf": date(2022, 12, 1),
        "wb_river_quaility_19012017.pdf": date(2016, 12, 1),
        "wb_NWMP_190823.pdf": date(2023, 7, 1),
        "wb_Water-NWMP-Dec-2018-24jan19.pdf": date(2018, 12, 1),
        "wb_river-quality-march_220520.pdf": date(2020, 3, 1),
    }
    for name, want in cases.items():
        assert month_from_name(name) == want, name
    assert upload_date("wb_NWMP_190823.pdf") == date(2023, 8, 19)


def test_month_from_header_text():
    assert month_from_text("... माह-सितम्बर-2022 ...") == date(2022, 9, 1)
    assert month_from_text("ekg fnlEcj&2018") == date(2018, 12, 1)  # legacy font encoding
    assert month_from_text("no month here 2022") is None


def test_header_month_rejected_when_after_upload():
    rep = Report(
        path=__import__("pathlib").Path("wb_water-june_190721.pdf"), month=None, month_source=""
    )
    decide_month(rep, "माह जून 2024")  # year misread; file was uploaded July 2021
    assert rep.month == date(2021, 6, 1) and rep.month_source == "file name"
    assert any("ignored" in n for n in rep.notes)


def test_interstate_values():
    assert parse_value("1(BDL)") == (1.0, True)
    assert parse_value("28×103") == (28000.0, False)
    assert parse_value("9×104") == (90000.0, False)
    assert parse_value("7.22") == (7.22, False)
    assert parse_value("Not Analysed") == (None, False)
    assert parse_value("Monitored Quarterly") == (None, False)


UP_HTML = """<table><tr><th>h</th></tr><tr><th>n</th></tr>
<tr>{a}</tr><tr>{b}</tr><tr>{c}</tr><tr><td>X Total</td><td>1</td></tr></table>"""


def _td(vals):
    return "".join(f"<td>{v}</td>" for v in vals)


def test_up_table_sums_rivers_per_town():
    full = [
        "65",
        "Varanasi",
        "Varanasi",
        "Assi",
        "20.63",
        "247.50",
        "1",
        "0",
        "1",
        "78",
        "0",
        "1442",
        "415000",
        "156300",
        "78",
        "2",
        "105",
        "95",
    ] + ["0"] * 16
    sub = ["Ganga", "29", "0", "29", "75.73", "101.47", "177.2", "4", "110", "103.5"] + ["0"] * 13
    town = [
        "Chunar",
        "Ganga",
        "0.40",
        "4.41",
        "0",
        "22",
        "22",
        "0",
        "0",
        "",
        "",
        "",
        "0",
        "0",
        "0",
        "0",
    ] + ["0"] * 16
    text = UP_HTML.format(a=_td(full), b=_td(sub), c=_td(town))
    towns = {t["town"]: t for t in parse_up_table(text)}
    v = towns["Varanasi"]
    assert v["rivers"] == ["Assi", "Ganga"]
    assert v["population_lakh"] == 20.63 and v["sewage_mld"] == 247.5
    assert v["stp_capacity_mld"] == 215.0 and v["stp_utilised_mld"] == 198.5
    assert towns["Chunar"]["sewage_mld"] == 4.41 and towns["Chunar"]["stp_capacity_mld"] == 0


def test_satellite_indices_use_only_water_pixels():
    scl = np.full((10, 10), 4)  # vegetation
    scl[:, :5] = 6  # left half water
    red = np.full((20, 20), 3000.0)
    green = np.full((20, 20), 1000.0)
    red[:, :10], green[:, :10] = 600.0, 400.0  # water: red > green means turbid
    got = indices(red, green, scl, 0.0)
    assert got["water_pixels"] == 200
    assert abs(got["ndti"] - 0.2) < 1e-9 and abs(got["red_reflectance"] - 0.06) < 1e-9
    assert indices(red, green, np.full((10, 10), 4), 0.0) is None  # no water in view
    dark = indices(np.full((20, 20), 1.0), np.full((20, 20), 1.0), np.full((10, 10), 6), 0.0)
    assert dark is None  # shadow / missing data rejected


def test_best_scene_per_month():
    def s(day, cloud):
        return {"properties": {"datetime": f"{day}T05:00:00Z", "eo:cloud_cover": cloud}}

    got = best_per_month([s("2024-03-05", 20), s("2024-03-15", 2), s("2024-04-01", 9)])
    assert [g["properties"]["datetime"][:10] for g in got] == ["2024-03-15", "2024-04-01"]
