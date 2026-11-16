"""Command line entry: python -m ganga.cli <command>"""

import argparse
from datetime import date
from pathlib import Path

from ganga.config import DATA_RAW
from ganga.db import connect

SCHEMA = Path(__file__).parent / "db" / "schema.sql"


def migrate(_args) -> None:
    with connect() as conn:
        conn.execute(SCHEMA.read_text())
    print("schema applied")


def network(_args) -> None:
    from ganga.ingest.cpcb_wqi import parse_file
    from ganga.network.build import (
        build_network,
        load_network,
        load_stations,
        write_cells,
        write_geojson,
    )

    net = build_network()
    write_geojson(net)
    write_cells(net)
    snaps = sorted((DATA_RAW / "cpcb" / "wqi").glob("*.html"))
    stations = parse_file(snaps[-1]) if snaps else []
    with connect() as conn:
        load_network(conn, net)
        n_main = load_stations(conn, net, stations)
    print(
        f"{len(net.nodes)} nodes over {net.length_km:.0f} km; "
        f"{len(stations)} stations, {n_main} on the modelled main stem"
    )


def pick_cells(_args) -> None:
    from ganga.ingest.drivers import pick_flow_cells
    from ganga.ingest.openmeteo import OpenMeteo

    with connect() as conn:
        for slug, mean in pick_flow_cells(conn, OpenMeteo(), date(2023, 8, 1), date(2023, 8, 31)):
            print(f"{slug:18s} mean Aug-2023 flow {mean:9.0f} m3/s")


def backfill(args) -> None:
    from ganga.ingest.drivers import backfill as run
    from ganga.ingest.openmeteo import OpenMeteo

    with connect() as conn:
        run(conn, OpenMeteo(), date.fromisoformat(args.start), date.today())


def daily(_args) -> None:
    from ganga.ingest.drivers import daily as run
    from ganga.ingest.openmeteo import OpenMeteo

    with connect() as conn:
        run(conn, OpenMeteo())


def observations(_args) -> None:
    from ganga.ingest import observations as obs
    from ganga.ingest.uppcb_run import read_registry

    reg = read_registry()
    with connect() as conn:
        conn.execute(SCHEMA.read_text())
        for name, fn in (
            ("UP monthly", obs.load_uppcb),
            ("CPCB interstate", obs.load_interstate),
            ("CPCB yearly", obs.load_yearly),
            ("CPCB map snapshot", obs.load_wqi),
        ):
            print(f"{name}: {fn(conn, reg)}")


def satellite(args) -> None:
    from ganga.ingest.sentinel import ingest

    with connect() as conn:
        n = ingest(conn, date.fromisoformat(args.start), date.today(), workers=args.workers)
    print(f"{n} node-month samples stored")


def loads(_args) -> None:
    from ganga.ingest.loads import build_loads, write_loads
    from ganga.network.build import build_network

    rows = build_loads(build_network())
    write_loads(rows)
    print(f"{len(rows)} towns written to data/curated/loads.csv")


def forecast(args) -> None:
    from ganga.forecast.run import issue

    with connect() as conn:
        conn.execute(SCHEMA.read_text())
        issue(conn, date.fromisoformat(args.date) if args.date else None)


def calibrate(args) -> None:
    from ganga.calibrate.run import main as run
    from ganga.calibrate.run import summary

    print(summary(run(maxiter=args.iterations)))


def verify(_args) -> None:
    from ganga.forecast.verify import verify as run

    with connect() as conn:
        out = run(conn)
    print(out or "no measurements yet fall in a month with a stored forecast")


def hindcast(_args) -> None:
    from ganga.forecast.hindcast import main as run

    run()


def seed_export(args) -> None:
    from ganga.db.seed import export_seed

    with connect() as conn:
        export_seed(conn, Path(args.dir))


def seed_import(args) -> None:
    from ganga.db.seed import import_seed

    with connect() as conn:
        conn.execute(SCHEMA.read_text())
        import_seed(conn, Path(args.dir))


def serve(args) -> None:
    import uvicorn

    from ganga.api.app import app, mount_frontend

    mount_frontend(app)
    # Behind the reverse proxy: trust its forwarded scheme and client address.
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")


def main() -> None:
    p = argparse.ArgumentParser(prog="ganga")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply the database schema").set_defaults(fn=migrate)
    sub.add_parser("network", help="build nodes, reaches and stations").set_defaults(fn=network)
    sub.add_parser("pick-cells", help="choose the GloFAS cell for each node").set_defaults(
        fn=pick_cells
    )
    b = sub.add_parser("backfill", help="load flow and weather history")
    b.add_argument("--start", default="2016-01-01")
    b.set_defaults(fn=backfill)
    sub.add_parser("daily", help="load recent days and forecasts").set_defaults(fn=daily)
    sub.add_parser("observations", help="load parsed water-quality observations").set_defaults(
        fn=observations
    )
    s = sub.add_parser("satellite", help="Sentinel-2 turbidity proxies at nodes")
    s.add_argument("--start", default="2019-01-01")
    s.add_argument("--workers", type=int, default=6)
    s.set_defaults(fn=satellite)
    sub.add_parser("loads", help="build the pollution-loads inventory").set_defaults(fn=loads)
    fc = sub.add_parser("forecast", help="issue today's forecast and alerts")
    fc.add_argument("--date", default=None)
    fc.set_defaults(fn=forecast)
    c = sub.add_parser("calibrate", help="fit model parameters to observations")
    c.add_argument("--iterations", type=int, default=60)
    c.set_defaults(fn=calibrate)
    sub.add_parser("hindcast", help="historical simulation and investment analysis").set_defaults(
        fn=hindcast
    )
    sub.add_parser("verify", help="score stored forecasts against later measurements").set_defaults(
        fn=verify
    )
    for name, fn, text in (
        ("seed-export", seed_export, "write loaded data to CSV files"),
        ("seed-import", seed_import, "load CSV files into an empty database"),
    ):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("dir")
        sp.set_defaults(fn=fn)
    sv = sub.add_parser("serve", help="run the API (and built dashboard)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8010)
    sv.set_defaults(fn=serve)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
