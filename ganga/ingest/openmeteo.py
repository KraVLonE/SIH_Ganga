"""Open-Meteo client: river discharge (GloFAS) and weather, for many points per request.

Free tier, no key. Requests are weighted by locations x days, so calls are batched and throttled;
a rate-limit reply raises RateLimited so a backfill can stop and be resumed later.
"""

import re
import time
from collections.abc import Iterator, Sequence
from datetime import date

import httpx

FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# our variable name -> Open-Meteo daily field
WEATHER_FIELDS = {
    "rain": "precipitation_sum",
    "air_temp": "temperature_2m_mean",
    "solar": "shortwave_radiation_sum",
}
_MEMBER = re.compile(r"river_discharge_member(\d+)$")

Point = tuple[float, float]  # (lon, lat)
Row = tuple[int, str, int, date, float]  # (point index, variable, member, day, value)


class RateLimited(RuntimeError):
    pass


def _get(client: httpx.Client, url: str, params: dict, pause: float) -> list[dict]:
    for attempt in range(4):
        r = client.get(url, params=params)
        if r.status_code == 429:
            reason = r.json().get("reason", "")
            if "inutely" in reason and attempt < 3:
                time.sleep(61)
                continue
            raise RateLimited(reason or "rate limited")
        if r.status_code >= 500 and attempt < 3:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(pause)
        data = r.json()
        return data if isinstance(data, list) else [data]
    raise RuntimeError("unreachable")


def _coords(points: Sequence[Point]) -> dict:
    return {
        "longitude": ",".join(f"{p[0]:.4f}" for p in points),
        "latitude": ",".join(f"{p[1]:.4f}" for p in points),
    }


def parse_daily(payload: list[dict], rename: dict[str, str]) -> Iterator[Row]:
    """rename maps Open-Meteo field -> our variable. Ensemble member fields become members 1..N."""
    for idx, loc in enumerate(payload):
        daily = loc.get("daily") or {}
        days = [date.fromisoformat(d) for d in daily.get("time", [])]
        for field, values in daily.items():
            if field == "time":
                continue
            m = _MEMBER.match(field)
            if m:
                variable, member = "flow", int(m.group(1))
            elif field in rename:
                variable, member = rename[field], 0
            else:
                continue
            for d, v in zip(days, values, strict=True):
                if v is not None:
                    yield idx, variable, member, d, float(v)


def grid_cells(payload: list[dict]) -> list[Point]:
    """The grid cell each requested point was matched to."""
    return [(loc["longitude"], loc["latitude"]) for loc in payload]


class OpenMeteo:
    def __init__(self, batch: int = 10, pause: float = 1.0, timeout: float = 120.0):
        self.batch, self.pause = batch, pause
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": "ganga-dss/0.1"})

    def _batched(self, url: str, points: Sequence[Point], params: dict) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(points), self.batch):
            chunk = points[i : i + self.batch]
            out.extend(_get(self.client, url, {**_coords(chunk), **params}, self.pause))
        return out

    def flow_history(self, points: Sequence[Point], start: date, end: date) -> list[dict]:
        return self._batched(
            FLOOD_URL,
            points,
            {
                "daily": "river_discharge",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )

    def flow_forecast(
        self, points: Sequence[Point], past_days: int = 14, days: int = 7
    ) -> list[dict]:
        return self._batched(
            FLOOD_URL,
            points,
            {
                "daily": "river_discharge",
                "past_days": past_days,
                "forecast_days": days,
                "ensemble": "true",
            },
        )

    def weather_history(self, points: Sequence[Point], start: date, end: date) -> list[dict]:
        return self._batched(
            ARCHIVE_URL,
            points,
            {
                "daily": ",".join(WEATHER_FIELDS.values()),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "timezone": "Asia/Kolkata",
            },
        )

    def weather_forecast(
        self, points: Sequence[Point], past_days: int = 14, days: int = 7
    ) -> list[dict]:
        return self._batched(
            FORECAST_URL,
            points,
            {
                "daily": ",".join(WEATHER_FIELDS.values()),
                "past_days": past_days,
                "forecast_days": days,
                "timezone": "Asia/Kolkata",
            },
        )
