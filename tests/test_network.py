import pytest
from shapely.geometry import LineString

from ganga.network.build import OSM_DUMP, build_network
from ganga.network.centreline import Centreline, stitch


def test_stitch_joins_out_of_order_and_reversed_pieces():
    a = LineString([(0, 0), (1000, 0)])
    b = LineString([(3000, 0), (2000, 0)])  # reversed, and 1 km gap from a
    c = LineString([(3000, 0), (4000, 0)])
    line = stitch([c, a, b])
    assert line.length == pytest.approx(4000)
    xs = [x for x, _ in line.coords]
    assert xs == sorted(xs) or xs == sorted(xs, reverse=True)


def test_stitch_leaves_out_far_pieces():
    a = LineString([(0, 0), (1000, 0)])
    far = LineString([(50_000, 0), (51_000, 0)])
    assert stitch([a, far]).length == pytest.approx(1000)


def test_snap_and_at_round_trip():
    # A straight west-east line near 80E, 26N; orientation hint at its west end.
    from ganga.network.centreline import _TO_M

    pts = [_TO_M.transform(80.0 + i * 0.01, 26.0) for i in range(101)]
    c = Centreline(LineString(pts))
    ch, off = c.snap(80.5, 26.0)
    assert off < 0.05
    assert ch == pytest.approx(c.length_km / 2, rel=0.01)
    lon, lat = c.at(ch)
    assert lon == pytest.approx(80.5, abs=1e-3) and lat == pytest.approx(26.0, abs=1e-3)
    _, off2 = c.snap(80.5, 26.05)
    assert off2 == pytest.approx(5.5, abs=0.3)  # 0.05 degrees of latitude


@pytest.mark.skipif(not OSM_DUMP.exists(), reason="OSM centreline not downloaded")
def test_real_network_is_ordered_and_sane():
    net = build_network()
    ch = [n.chainage_km for n in net.nodes]
    assert ch == sorted(ch)
    assert net.nodes[0].slug == "haridwar" and net.nodes[-1].slug == "patna"
    assert 1200 < net.length_km < 1500
    slugs = {n.slug for n in net.nodes}
    assert {"yamuna", "yamuna_up", "yamuna_dn"} <= slugs
    # Sangam is just below the Rasoolabad station and above Vindhyachal.
    by = {n.slug: n.chainage_km for n in net.nodes}
    assert by["prayagraj_us"] < by["yamuna"] < by["vindhyachal"]
    # Reaches tile the whole river with no gaps.
    assert sum(b.chainage_km - a.chainage_km for a, b, _ in net.reaches()) == pytest.approx(
        net.length_km
    )
