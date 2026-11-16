"""River centreline from an OpenStreetMap relation dump, as one projected line.

Chainage is distance along this line in km, measured downstream from its upstream end.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import linemerge

# UTM 44N covers the Haridwar-Patna stretch (78E-84E) with small distortion to ~86E.
_TO_M = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)
_TO_LL = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)

MAX_GAP_M = 3000.0


def stitch(lines: list[LineString], max_gap_m: float = MAX_GAP_M) -> LineString:
    """Join segments end to end into one line, bridging small gaps between pieces."""
    merged = linemerge(lines)
    pieces = [merged] if merged.geom_type == "LineString" else list(merged.geoms)
    # Start from the longest piece and greedily attach whichever piece has the nearest endpoint.
    pieces.sort(key=lambda g: g.length, reverse=True)
    coords = list(pieces.pop(0).coords)
    while pieces:
        best = None
        for i, p in enumerate(pieces):
            pc = list(p.coords)
            for at_end, mine in ((True, coords[-1]), (False, coords[0])):
                for flip, theirs in ((False, pc[0]), (True, pc[-1])):
                    d = Point(mine).distance(Point(theirs))
                    if best is None or d < best[0]:
                        best = (d, i, at_end, flip)
        d, i, at_end, flip = best
        if d > max_gap_m:
            break  # remaining pieces are side channels or disconnected; leave them out
        pc = list(pieces.pop(i).coords)
        if flip:
            pc.reverse()
        coords = coords + pc if at_end else pc[::-1] + coords
    return LineString(coords)


@dataclass
class Centreline:
    line: LineString  # projected metres, oriented upstream -> downstream

    @classmethod
    def from_osm(cls, path: Path, upstream_hint: tuple[float, float]) -> "Centreline":
        """upstream_hint is (lon, lat) of a place near the upstream end, used for orientation."""
        data = json.loads(Path(path).read_text())
        rel = next(e for e in data["elements"] if e["type"] == "relation")
        main = {m["ref"] for m in rel["members"] if m["role"] == "main_stream"}
        lines = [
            LineString([_TO_M.transform(p["lon"], p["lat"]) for p in e["geometry"]])
            for e in data["elements"]
            if e["type"] == "way" and e["id"] in main
        ]
        line = stitch(lines)
        hint = Point(_TO_M.transform(*upstream_hint))
        if Point(line.coords[0]).distance(hint) > Point(line.coords[-1]).distance(hint):
            line = LineString(list(line.coords)[::-1])
        return cls(line)

    @property
    def length_km(self) -> float:
        return self.line.length / 1000.0

    def snap(self, lon: float, lat: float) -> tuple[float, float]:
        """Return (chainage_km, offset_km) of the nearest point on the line."""
        pt = Point(_TO_M.transform(lon, lat))
        return self.line.project(pt) / 1000.0, self.line.distance(pt) / 1000.0

    def at(self, chainage_km: float) -> tuple[float, float]:
        """(lon, lat) at a chainage."""
        p = self.line.interpolate(chainage_km * 1000.0)
        return _TO_LL.transform(p.x, p.y)

    def lonlat(self, start_km: float, end_km: float, step_km: float = 1.0) -> list[list[float]]:
        n = max(2, int((end_km - start_km) / step_km) + 1)
        out = []
        for i in range(n):
            lon, lat = self.at(start_km + (end_km - start_km) * i / (n - 1))
            out.append([round(lon, 5), round(lat, 5)])
        return out
