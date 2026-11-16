"""Pollution-loads inventory: sewage generated and treated per town along the river.

Sources
- UP: the state's Namami Gange "Drains / STPs comparison" table (official, 2024 figures).
- Uttarakhand and Bihar: CPCB/NMCG filings to the National Green Tribunal for treatment
  capacity; sewage generation there is estimated from population and is flagged as such.

Town coordinates are approximate town centres entered by hand; the offset from the river is
reported so far-from-bank towns can be down-weighted by the model.
"""

import csv
import html
import re
from pathlib import Path

from ganga.config import DATA_CURATED, DATA_RAW

UP_TABLE = DATA_RAW / "up_namami"
OUT = DATA_CURATED / "loads.csv"
UP_URL = "https://www.jjm.up.gov.in/NamamiGange/Drains_STPs_Comparison"
LPCD = 108.0  # litres of sewage per person per day implied by the UP table for towns

# Approximate town centres (lon, lat).
COORDS = {
    "Bijnor": (78.136, 29.372),
    "Garmukteshwar": (78.103, 28.787),
    "Anupshahr(NPP)": (78.270, 28.357),
    "Narora": (78.381, 28.197),
    "Amroha": (78.467, 28.904),
    "Sambhal": (78.570, 28.585),
    "Farrukhabad cum Fatehgarh": (79.580, 27.390),
    "Fatehgarh": (79.632, 27.365),
    "Kannauj": (79.920, 27.055),
    "Bilhaur": (80.063, 26.845),
    "Bithoor": (80.272, 26.613),
    "Kanpur Nagar": (80.345, 26.465),
    "Shuklaganj NPP": (80.385, 26.478),
    "Unnao NPP": (80.488, 26.547),
    "Fatehpur (NPP)": (80.813, 25.930),
    "Dalmau": (81.030, 26.060),
    "Manikpur": (81.380, 25.770),
    "Daranagar": (81.350, 25.680),
    "Prayagraj (M. Corp)": (81.846, 25.435),
    "Mirzapur": (82.569, 25.146),
    "Chunar": (82.884, 25.127),
    "Varanasi": (83.010, 25.317),
    "Mugalsarai": (83.119, 25.283),
    "Saidpur": (83.222, 25.537),
    "Zamania": (83.557, 25.419),
    "Ghazipur": (83.577, 25.580),
    "Ballia": (84.148, 25.760),
}

NGT_CPCB = "CPCB compliance report to NGT, OA 200/2014 (2024), Table 3 (data/raw/ngt/)"
NGT_NMCG = "NMCG affidavit to NGT, OA 491/2024, Annexure I (data/raw/ngt/)"

# Towns outside UP. population_lakh: Census 2011 figure recalled from memory and projected to
# 2024 at 1.7 %/yr - NOT verified against the census tables; sewage = population x LPCD.
MANUAL = [
    {
        "town": "Haridwar",
        "state": "Uttarakhand",
        "lon": 78.164,
        "lat": 29.945,
        "population_lakh": None,
        "sewage_mld": 151.5,
        "stp_capacity_mld": 145.0,
        "stp_utilised_mld": 151.5,
        "source": NGT_CPCB,
        "confidence": "partial",
        "note": "Haridwar district: 5 plants. Generation not published; set to measured plant "
        "inflow, so untreated flow is unknown (pilgrim peaks not captured).",
    },
    {
        "town": "Buxar",
        "state": "Bihar",
        "lon": 83.978,
        "lat": 25.564,
        "population_lakh": 1.28,
        "sewage_mld": None,
        "stp_capacity_mld": 0.0,
        "stp_utilised_mld": 0.0,
        "source": NGT_NMCG,
        "confidence": "estimated",
        "note": "No operational plant listed for Buxar in the 2024 filing. Population recalled.",
    },
    {
        "town": "Chhapra",
        "state": "Bihar",
        "lon": 84.727,
        "lat": 25.780,
        "population_lakh": 2.51,
        "sewage_mld": None,
        "stp_capacity_mld": 0.0,
        "stp_utilised_mld": 0.0,
        "source": NGT_NMCG,
        "confidence": "estimated",
        "note": "No operational plant listed for Chhapra in the 2024 filing. Population recalled.",
    },
    {
        "town": "Patna",
        "state": "Bihar",
        "lon": 85.137,
        "lat": 25.611,
        "population_lakh": 25.5,
        "sewage_mld": None,
        "stp_capacity_mld": 200.0,
        "stp_utilised_mld": 95.0,
        "source": NGT_NMCG,
        "confidence": "estimated",
        "note": "Plants: Beur 43, Karmalichak 37, Pahari 60, Saidpur 60 MLD (designed). Utilised "
        "set to 47.5 % of designed, the Bihar-wide figure in the filing. Urban area "
        "population recalled. Much of the city drains below the model's end at Gandhi Ghat.",
    },
]

_TR = re.compile(r"<tr.*?</tr>", re.S)
_TD = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)


def _cells(row: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
        for c in _TD.findall(row)
    ]


def _f(text: str) -> float:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return 0.0


def parse_up_table(text: str) -> list[dict]:
    """Towns in the UP table, with figures summed over the rivers each town drains to.

    Row shapes: 34 cells (serial, district, town, river, ...), 32 cells (town, river, ...; same
    district), 23 cells (river, ...; a further river for the same town). Total rows are skipped.
    """
    towns: list[dict] = []
    cur = None
    for row in _TR.findall(text)[2:]:
        c = _cells(row)
        if not c or "Total" in " ".join(c[:3]):
            continue
        if len(c) in (34, 32):
            off = 2 if len(c) == 34 else 0
            cur = {
                "town": c[off],
                "rivers": [c[off + 1]],
                "population_lakh": _f(c[off + 2]),
                "sewage_mld": _f(c[off + 3]),
                "drain_discharge_mld": _f(c[off + 7]),
                "stp_capacity_mld": _f(c[off + 14]),
                "stp_utilised_mld": _f(c[off + 15]),
            }
            towns.append(cur)
        elif len(c) == 23 and cur is not None:
            cur["rivers"].append(c[0])
            cur["drain_discharge_mld"] += _f(c[4])
            cur["stp_capacity_mld"] += _f(c[8])
            cur["stp_utilised_mld"] += _f(c[9])
    return towns


def build_loads(net) -> list[dict]:
    """Inventory rows for towns draining to the modelled Ganga, ordered downstream."""
    snaps = sorted(UP_TABLE.glob("*.html"))
    rows = []
    if snaps:
        text = snaps[-1].read_text(encoding="utf-8", errors="replace")
        for t in parse_up_table(text):
            if "Ganga" not in t["rivers"] or t["town"] not in COORDS:
                continue
            if t["population_lakh"] == 0 and t["sewage_mld"] == 0:
                # e.g. Fatehgarh: only its plant is listed separately; fold into the main town.
                main = next((r for r in rows if "Fatehgarh" in r["town"]), None)
                if main and t["town"] == "Fatehgarh":
                    main["stp_capacity_mld"] += t["stp_capacity_mld"]
                    main["stp_utilised_mld"] += t["stp_utilised_mld"]
                continue
            lon, lat = COORDS[t["town"]]
            rows.append(
                {
                    "town": t["town"],
                    "state": "Uttar Pradesh",
                    "lon": lon,
                    "lat": lat,
                    "rivers": "+".join(t["rivers"]),
                    "population_lakh": t["population_lakh"],
                    "sewage_mld": t["sewage_mld"],
                    "drain_discharge_mld": round(t["drain_discharge_mld"], 2),
                    "stp_capacity_mld": round(t["stp_capacity_mld"], 2),
                    "stp_utilised_mld": round(t["stp_utilised_mld"], 2),
                    "source": f"UP Namami Gange drains/STP table ({snaps[-1].name}); {UP_URL}",
                    "confidence": "official",
                    "note": "",
                }
            )
    for m in MANUAL:
        r = {"rivers": "Ganga", "drain_discharge_mld": "", **m}
        if r["sewage_mld"] is None:
            r["sewage_mld"] = round(r["population_lakh"] * 1e5 * LPCD / 1e6, 1)
        rows.append(r)
    for r in rows:
        ch, off = net.chainage(r["lon"], r["lat"])
        r["chainage_km"], r["offset_km"] = round(ch, 1), round(off, 1)
        # Flow that reaches the river untreated, before any in-stream decay.
        r["untreated_mld"] = round(
            max(0.0, r["sewage_mld"] - min(r["stp_utilised_mld"], r["sewage_mld"])), 1
        )
    rows = [r for r in rows if -5 <= r["chainage_km"] <= net.length_km + 5]
    rows.sort(key=lambda r: r["chainage_km"])
    return rows


FIELDS = [
    "town",
    "state",
    "rivers",
    "chainage_km",
    "offset_km",
    "lon",
    "lat",
    "population_lakh",
    "sewage_mld",
    "stp_capacity_mld",
    "stp_utilised_mld",
    "untreated_mld",
    "drain_discharge_mld",
    "confidence",
    "source",
    "note",
]


def write_loads(rows: list[dict], path: Path = OUT) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
