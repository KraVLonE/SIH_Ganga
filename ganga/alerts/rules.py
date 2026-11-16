"""Alert rules evaluated on each forecast run.

Much of the middle Ganga already fails the bathing limit for coliform on a normal day, so
alerting on "limit exceeded" alone would fire constantly. The rules therefore look for change:

deterioration  a place forecast to get clearly worse than its own last ten days
pulse          a slug of unusually polluted water, with when it reaches places downstream
storm          rain heavy enough to overflow sewers at a town, with downstream arrival times
breach         a place that has been meeting the bathing limits is likely to stop meeting them
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import httpx
import numpy as np

from ganga.forecast.run import HISTORY_DAYS, LIMITS, Ensemble

DETERIORATION = {"fc": 2.0, "bod": 1.5}  # forecast / recent ratio that counts as worse
DO_DROP = 1.0  # mg/L fall in oxygen that counts as worse
PULSE_RATIO = 2.0  # concentration relative to the recent level at the same place
PULSE_MIN_KM = 15.0
STORM_RAIN_MM = 25.0
BREACH_PROBABILITY = 0.6
QUIET_DAYS = 3  # do not repeat the same alert for the same place within this many days
LABEL = {"fc": "fecal coliform", "bod": "BOD", "do": "dissolved oxygen"}
UNIT = {"fc": "MPN/100 mL", "bod": "mg/L", "do": "mg/L"}


@dataclass
class Alert:
    kind: str
    severity: str  # info | warning | severe
    chainage_km: float
    valid_on: date
    message: str
    details: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.details.get('place', round(self.chainage_km))}"


def _places(conn) -> list[tuple[str, float]]:
    return conn.execute(
        "SELECT name, chainage_km FROM nodes WHERE kind = 'node' ORDER BY chainage_km"
    ).fetchall()


def _towns(conn_unused=None) -> list[dict]:
    from ganga.model.setup import _towns as load

    return load()


def arrival_days(travel: np.ndarray, from_cell: int, to_cell: int) -> float:
    """Days for water to move between two cells, from cumulative travel time on one day."""
    return float(travel[to_cell] - travel[from_cell])


def downstream_arrivals(
    ens: Ensemble, day: int, cell: int, places, limit_days: float = 7.0
) -> list[dict]:
    out = []
    for name, chainage in places:
        to = ens.grid.cell(chainage)
        if to <= cell:
            continue
        eta = arrival_days(ens.travel[day], cell, to)
        if eta > limit_days:
            break
        out.append(
            {
                "place": name,
                "days": round(eta, 1),
                "date": (ens.dates[day] + timedelta(days=round(eta))).isoformat(),
            }
        )
    return out


def _fmt(variable: str, value: float) -> str:
    return f"{value:,.0f}" if variable == "fc" else f"{value:.1f}"


def deterioration(ens: Ensemble, summary: dict, places) -> list[Alert]:
    out = []
    past, future = slice(0, HISTORY_DAYS), slice(HISTORY_DAYS, None)
    for name, chainage in places:
        c = ens.grid.cell(chainage)
        for v in ("fc", "bod", "do"):
            p50 = summary[v]["p50"][:, c]
            recent = float(np.median(p50[past]))
            if v == "do":
                worst = int(np.argmin(p50[future]))
                value = float(p50[future][worst])
                bad = recent - value >= DO_DROP
            else:
                worst = int(np.argmax(p50[future]))
                value = float(p50[future][worst])
                bad = value >= DETERIORATION[v] * recent and (value > LIMITS[v][1] * 0.5)
            if not bad:
                continue
            when = ens.dates[HISTORY_DAYS + worst]
            breach = (value < LIMITS[v][1]) if v == "do" else (value > LIMITS[v][1])
            verb = "fall" if v == "do" else "rise"
            out.append(
                Alert(
                    "deterioration",
                    "severe" if breach else "warning",
                    float(chainage),
                    when,
                    f"{name}: {LABEL[v]} forecast to {verb} from about {_fmt(v, recent)} to "
                    f"{_fmt(v, value)} {UNIT[v]} by {when:%d %b}"
                    + (" (outside the bathing limit)." if breach else "."),
                    {"place": name, "variable": v, "recent": recent, "forecast": value},
                )
            )
    return out


def pulses(ens: Ensemble, summary: dict, places) -> list[Alert]:
    """Stretches where the forecast is far above the recent level at the same place."""
    out = []
    dx = ens.grid.dx_km
    for v in ("fc", "bod"):
        p50 = summary[v]["p50"]
        recent = np.median(p50[:HISTORY_DAYS], axis=0)
        for d in range(HISTORY_DAYS, len(ens.dates)):
            hot = (p50[d] > PULSE_RATIO * np.maximum(recent, 1e-9)) & (p50[d] > 0.5 * LIMITS[v][1])
            if not hot.any():
                continue
            edges = np.flatnonzero(np.diff(np.concatenate(([0], hot.astype(int), [0]))))
            for a, b in zip(edges[::2], edges[1::2], strict=True):
                if (b - a) * dx < PULSE_MIN_KM:
                    continue
                peak = a + int(np.argmax(p50[d, a:b] / np.maximum(recent[a:b], 1e-9)))
                front = b - 1
                arrivals = downstream_arrivals(ens, d, front, places)
                where = min(places, key=lambda pl: abs(pl[1] - ens.grid.centres[peak]))[0]
                msg = (
                    f"Pulse of high {LABEL[v]} near {where} on {ens.dates[d]:%d %b}: about "
                    f"{_fmt(v, p50[d, peak])} {UNIT[v]}, {p50[d, peak] / recent[peak]:.1f}x the "
                    f"recent level, over {(b - a) * dx:.0f} km."
                )
                if arrivals:
                    msg += (
                        " Expected to reach "
                        + ", ".join(f"{x['place']} in {x['days']:g} d" for x in arrivals[:3])
                        + "."
                    )
                out.append(
                    Alert(
                        "pulse",
                        "severe",
                        float(ens.grid.centres[peak]),
                        ens.dates[d],
                        msg,
                        {
                            "place": where,
                            "variable": v,
                            "km_from": float(a * dx),
                            "km_to": float(b * dx),
                            "arrivals": arrivals,
                        },
                    )
                )
            break  # report each variable's pulse on the first day it appears
    return out


def storms(ens: Ensemble, places) -> list[Alert]:
    out = []
    for t in _towns():
        treated = min(float(t["stp_utilised_mld"]), float(t["sewage_mld"]))
        if float(t["sewage_mld"]) < 10:
            continue
        c = ens.grid.cell(float(t["chainage_km"]))
        rain = ens.rain[HISTORY_DAYS:, c]
        d = int(np.argmax(rain))
        if rain[d] < STORM_RAIN_MM:
            continue
        day = HISTORY_DAYS + d
        arrivals = downstream_arrivals(ens, day, c, places)
        msg = (
            f"{t['town']}: {rain[d]:.0f} mm of rain forecast on {ens.dates[day]:%d %b}; "
            f"sewer overflow and street wash-off likely"
            + (f" ({treated:.0f} MLD normally treated)." if treated else ".")
        )
        if arrivals:
            msg += (
                " Polluted water would reach "
                + ", ".join(f"{x['place']} in {x['days']:g} d" for x in arrivals[:3])
                + "."
            )
        out.append(
            Alert(
                "storm",
                "warning",
                float(t["chainage_km"]),
                ens.dates[day],
                msg,
                {"place": t["town"], "rain_mm": float(rain[d]), "arrivals": arrivals},
            )
        )
    return out


def breaches(ens: Ensemble, summary: dict, places) -> list[Alert]:
    out = []
    p = summary["p_unfit"]["p50"]
    for name, chainage in places:
        c = ens.grid.cell(chainage)
        was = float(np.mean(p[:HISTORY_DAYS, c]))
        ahead = p[HISTORY_DAYS:, c]
        d = int(np.argmax(ahead))
        if was < 0.4 and ahead[d] >= BREACH_PROBABILITY:
            when = ens.dates[HISTORY_DAYS + d]
            out.append(
                Alert(
                    "breach",
                    "warning",
                    float(chainage),
                    when,
                    f"{name}: {ahead[d]:.0%} chance of failing the bathing limits on {when:%d %b} "
                    f"(about {was:.0%} over the last ten days).",
                    {"place": name, "probability": float(ahead[d]), "recent_probability": was},
                )
            )
    return out


def evaluate(conn, today: date, ens: Ensemble, summary: dict) -> list[Alert]:
    places = _places(conn)
    return (
        pulses(ens, summary, places)
        + deterioration(ens, summary, places)
        + storms(ens, places)
        + breaches(ens, summary, places)
    )


def record(conn, run_id: int, alerts: list[Alert], send: bool = True) -> int:
    """Store alerts; skip ones already raised for the same place in the last few days."""
    new = []
    for a in alerts:
        seen = conn.execute(
            "SELECT 1 FROM alerts WHERE kind = %s AND details->>'key' = %s "
            "AND created_at > now() - make_interval(days => %s) LIMIT 1",
            (a.kind, a.key, QUIET_DAYS),
        ).fetchone()
        repeat = seen is not None
        conn.execute(
            "INSERT INTO alerts (run_id, kind, severity, chainage_km, valid_on, message, details, "
            "delivered) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                a.kind,
                a.severity,
                a.chainage_km,
                a.valid_on,
                a.message,
                json.dumps({**a.details, "key": a.key, "repeat": repeat}),
                False,
            ),
        )
        if not repeat:
            new.append(a)
    conn.commit()
    if send and new and deliver(new):
        conn.execute(
            "UPDATE alerts SET delivered = TRUE WHERE run_id = %s "
            "AND NOT (details->>'repeat')::boolean",
            (run_id,),
        )
        conn.commit()
    return len(new)


def deliver(alerts: list[Alert]) -> bool:
    """Send a digest to Telegram if a bot token and chat id are configured."""
    token, chat = os.environ.get("GANGA_TELEGRAM_TOKEN"), os.environ.get("GANGA_TELEGRAM_CHAT")
    if not (token and chat):
        return False
    order = {"severe": 0, "warning": 1, "info": 2}
    lines = ["Ganga water quality alerts"]
    for a in sorted(alerts, key=lambda a: (order.get(a.severity, 3), a.chainage_km))[:15]:
        lines.append(f"[{a.severity}] {a.message}")
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": "\n\n".join(lines)},
            timeout=20,
        )
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def as_dict(a: Alert) -> dict:
    d = asdict(a)
    d["valid_on"] = a.valid_on.isoformat()
    return d
