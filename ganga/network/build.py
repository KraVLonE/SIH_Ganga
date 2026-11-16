"""Build the river network: seed places -> nodes with chainage, reaches, and station chainages."""

import csv
import json
from dataclasses import asdict, dataclass

from ganga.config import DATA_CURATED, DATA_RAW
from ganga.network.centreline import Centreline

OSM_DUMP = DATA_RAW / "osm" / "ganga.json"
SEED = DATA_CURATED / "nodes_seed.csv"
HARIDWAR = (78.171, 29.956)
MAX_SEED_OFFSET_KM = 8.0
MAX_STATION_OFFSET_KM = 5.0
FLOWPOINT_GAP_KM = 8.0  # virtual flow points this far above and below each confluence


@dataclass
class Node:
    slug: str
    name: str
    kind: str
    chainage_km: float
    lon: float
    lat: float
    note: str = ""


@dataclass
class Network:
    centreline: Centreline
    origin_km: float  # centreline chainage of Haridwar
    nodes: list[Node]

    @property
    def length_km(self) -> float:
        return self.nodes[-1].chainage_km

    def chainage(self, lon: float, lat: float) -> tuple[float, float]:
        """(km downstream of Haridwar, offset from the river in km)."""
        ch, off = self.centreline.snap(lon, lat)
        return ch - self.origin_km, off

    def position(self, chainage_km: float) -> tuple[float, float]:
        return self.centreline.at(chainage_km + self.origin_km)

    def reaches(self) -> list[tuple[Node, Node, list[list[float]]]]:
        out = []
        for a, b in zip(self.nodes, self.nodes[1:], strict=False):
            coords = self.centreline.lonlat(
                a.chainage_km + self.origin_km, b.chainage_km + self.origin_km
            )
            out.append((a, b, coords))
        return out


def build_network() -> Network:
    line = Centreline.from_osm(OSM_DUMP, HARIDWAR)
    origin, _ = line.snap(*HARIDWAR)
    nodes: list[Node] = []
    with SEED.open() as fh:
        for r in csv.DictReader(fh):
            ch, off = line.snap(float(r["lon"]), float(r["lat"]))
            if off > MAX_SEED_OFFSET_KM:
                raise ValueError(f"seed {r['slug']} is {off:.1f} km from the river")
            ch -= origin
            lon, lat = line.at(ch + origin)
            nodes.append(Node(r["slug"], r["name"], r["kind"], round(ch, 2), lon, lat, r["note"]))
    for c in [n for n in nodes if n.kind == "confluence"]:
        for suffix, label, d in (
            ("up", "above", -FLOWPOINT_GAP_KM),
            ("dn", "below", FLOWPOINT_GAP_KM),
        ):
            ch = round(c.chainage_km + d, 2)
            lon, lat = line.at(ch + origin)
            trib = c.name.replace(" confluence", "")
            nodes.append(
                Node(f"{c.slug}_{suffix}", f"{label.title()} {trib}", "flowpoint", ch, lon, lat)
            )
    nodes.sort(key=lambda n: n.chainage_km)
    chainages = [n.chainage_km for n in nodes]
    if len(set(chainages)) != len(chainages):
        raise ValueError("two nodes share a chainage")
    return Network(line, origin, nodes)


def write_geojson(net: Network) -> None:
    feats = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(n.lon, 5), round(n.lat, 5)]},
            "properties": {k: v for k, v in asdict(n).items() if k not in ("lon", "lat")},
        }
        for n in net.nodes
    ]
    for a, b, coords in net.reaches():
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "from": a.slug,
                    "to": b.slug,
                    "length_km": round(b.chainage_km - a.chainage_km, 2),
                },
            }
        )
    out = DATA_CURATED / "network.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def write_cells(net: Network, dx_km: float = 5.0) -> None:
    """The river cut into model cells, each a short line, for colouring the map."""
    feats = []
    n = int(-(-net.length_km // dx_km))
    for i in range(n):
        a, b = i * dx_km, min((i + 1) * dx_km, net.length_km)
        coords = net.centreline.lonlat(a + net.origin_km, b + net.origin_km, step_km=0.5)
        feats.append(
            {
                "type": "Feature",
                "id": i,
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"cell": i, "chainage_km": round((i + 0.5) * dx_km, 2)},
            }
        )
    out = DATA_CURATED / "river_cells.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def load_network(conn, net: Network) -> None:
    with conn.cursor() as cur:
        for n in net.nodes:
            cur.execute(
                """
                INSERT INTO nodes (slug, name, kind, chainage_km, lon, lat, note, geom)
                VALUES (%s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                    chainage_km = EXCLUDED.chainage_km, lon = EXCLUDED.lon, lat = EXCLUDED.lat,
                    note = EXCLUDED.note, geom = EXCLUDED.geom
                """,
                (n.slug, n.name, n.kind, n.chainage_km, n.lon, n.lat, n.note, n.lon, n.lat),
            )
        cur.execute("DELETE FROM nodes WHERE slug <> ALL(%s)", ([n.slug for n in net.nodes],))
        cur.execute("DELETE FROM reaches")
        for a, b, coords in net.reaches():
            cur.execute(
                """
                INSERT INTO reaches (from_node, to_node, length_km, geom)
                SELECT a.id, b.id, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)
                FROM nodes a, nodes b WHERE a.slug = %s AND b.slug = %s
                """,
                (
                    round(b.chainage_km - a.chainage_km, 2),
                    json.dumps({"type": "LineString", "coordinates": coords}),
                    a.slug,
                    b.slug,
                ),
            )
    conn.commit()


def load_stations(conn, net: Network, stations: list[dict]) -> int:
    """stations: dicts with code, name, lon, lat. Sets chainage only for main-stem stations."""
    n_main = 0
    with conn.cursor() as cur:
        for s in stations:
            ch, off = net.chainage(s["lon"], s["lat"])
            on_stem = (
                off <= MAX_STATION_OFFSET_KM
                and -1.0 <= ch <= net.length_km + 1.0
                and "GANGA" in s["name"].upper()
            )
            n_main += on_stem
            cur.execute(
                """
                INSERT INTO stations
                    (code, name, river, state, lon, lat, chainage_km, offset_km, geom)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, river = EXCLUDED.river,
                    state = EXCLUDED.state, lon = EXCLUDED.lon, lat = EXCLUDED.lat,
                    chainage_km = EXCLUDED.chainage_km, offset_km = EXCLUDED.offset_km,
                    geom = EXCLUDED.geom
                """,
                (
                    s["code"],
                    s["name"],
                    s.get("river"),
                    s.get("state"),
                    s["lon"],
                    s["lat"],
                    round(ch, 2) if on_stem else None,
                    round(off, 2),
                    s["lon"],
                    s["lat"],
                ),
            )
    conn.commit()
    return n_main
