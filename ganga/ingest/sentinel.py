"""Sentinel-2 turbidity proxies at river nodes, from the public Earth Search catalogue (no key).

For each node and month we take the least cloudy scene, read a ~1 km window around the node,
keep only pixels the scene classification marks as water, and store:
- ndti: Normalised Difference Turbidity Index (red - green) / (red + green); higher = muddier
- red_reflectance: red-band surface reflectance over water (rises with suspended sediment)

These are proxies for suspended sediment, not measurements of BOD, coliform or nutrients.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

STAC = "https://earth-search.aws.element84.com/v1/search"
HALF_M = 500.0  # half-width of the sampling window
MIN_WATER_PIXELS = 30  # at 10 m, i.e. at least 3000 m2 of water
WATER = 6  # scene-classification code for water
GDAL_ENV = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_TIMEOUT": "60",
}


def search(lon: float, lat: float, start: date, end: date, max_cloud: float = 30.0) -> list[dict]:
    """All scenes over a point in a date range, below a cloud-cover limit."""
    body = {
        "collections": ["sentinel-2-l2a"],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "limit": 100,
    }
    out = []
    with httpx.Client(timeout=90) as client:
        url, payload = STAC, body
        while url:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            out.extend(data["features"])
            nxt = next((ln for ln in data.get("links", []) if ln["rel"] == "next"), None)
            url, payload = (nxt["href"], nxt.get("body", payload)) if nxt else (None, None)
    return out


def best_per_month(scenes: list[dict]) -> list[dict]:
    """The least cloudy scene of each calendar month."""
    best: dict[str, dict] = {}
    for s in scenes:
        key = s["properties"]["datetime"][:7]
        if key not in best or (
            s["properties"]["eo:cloud_cover"] < best[key]["properties"]["eo:cloud_cover"]
        ):
            best[key] = s
    return [best[k] for k in sorted(best)]


def _window(src, x: float, y: float, half_m: float) -> Window:
    row, col = src.index(x, y)
    n = int(round(half_m / abs(src.transform.a)))
    return Window(col - n, row - n, 2 * n + 1, 2 * n + 1)


def _read(href: str, x: float, y: float) -> np.ndarray:
    with rasterio.open(href) as src:
        return src.read(1, window=_window(src, x, y, HALF_M), boundless=True, fill_value=0)


def indices(red: np.ndarray, green: np.ndarray, scl: np.ndarray, offset: float) -> dict | None:
    """Turbidity proxies over water pixels. scl is at half the resolution of red/green."""
    mask = np.kron(scl == WATER, np.ones((2, 2), dtype=bool))
    h, w = min(mask.shape[0], red.shape[0]), min(mask.shape[1], red.shape[1])
    mask, red, green = mask[:h, :w], red[:h, :w].astype(float), green[:h, :w].astype(float)
    mask &= (red > 0) & (green > 0)
    if mask.sum() < MIN_WATER_PIXELS:
        return None
    r = (red[mask] + offset) / 10000.0
    g = (green[mask] + offset) / 10000.0
    ok = (r > 0) & (g > 0)
    if ok.sum() < MIN_WATER_PIXELS:
        return None
    r, g = r[ok], g[ok]
    # Near-zero or very bright "water" is shadow, missing data or cloud that slipped the mask.
    if not 0.005 <= float(np.median(r)) <= 0.35:
        return None
    return {
        "ndti": float(np.median((r - g) / (r + g))),
        "red_reflectance": float(np.median(r)),
        "water_pixels": int(ok.sum()),
    }


def sample(scene: dict, lon: float, lat: float) -> dict | None:
    props, assets = scene["properties"], scene["assets"]
    epsg = props.get("proj:epsg") or int(str(props.get("proj:code", "EPSG:0")).split(":")[1])
    x, y = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform(lon, lat)
    # From processing baseline 04.00 the digital numbers carry a +1000 offset unless the
    # catalogue says it has already been removed.
    applied = props.get("earthsearch:boa_offset_applied", False)
    baseline = float(props.get("s2:processing_baseline", "0") or 0)
    offset = -1000.0 if (baseline >= 4.0 and not applied) else 0.0
    with rasterio.Env(**GDAL_ENV):
        scl = _read(assets["scl"]["href"], x, y)
        if (scl == WATER).sum() * 4 < MIN_WATER_PIXELS:
            return None
        red = _read(assets["red"]["href"], x, y)
        green = _read(assets["green"]["href"], x, y)
    out = indices(red, green, scl, offset)
    if out:
        out["time"] = date.fromisoformat(props["datetime"][:10])
        out["scene"] = scene["id"]
    return out


def node_series(lon: float, lat: float, start: date, end: date) -> Iterator[dict]:
    for scene in best_per_month(search(lon, lat, start, end)):
        try:
            got = sample(scene, lon, lat)
        except Exception:  # a missing or unreadable tile should not stop the run
            got = None
        if got:
            yield got


def ingest(conn, start: date, end: date, workers: int = 8, log=print) -> int:
    nodes = conn.execute(
        "SELECT id, slug, lon, lat FROM nodes WHERE kind = 'node' ORDER BY chainage_km"
    ).fetchall()

    def run(node):
        return node, list(node_series(node[2], node[3], start, end))

    total = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (node_id, slug, _, _), rows in pool.map(run, nodes):
            with conn.cursor() as cur:
                for r in rows:
                    for var in ("ndti", "red_reflectance"):
                        cur.execute(
                            """
                            INSERT INTO timeseries (node_id, variable, source, member, time, value)
                            VALUES (%s, %s, 'sentinel2', 0, %s, %s)
                            ON CONFLICT (node_id, variable, source, member, time)
                            DO UPDATE SET value = EXCLUDED.value
                            """,
                            (node_id, var, r["time"], r[var]),
                        )
            conn.commit()
            total += len(rows)
            log(f"{slug}: {len(rows)} monthly scenes")
    return total
