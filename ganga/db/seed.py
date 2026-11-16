"""Copy the loaded data (network, drivers, measurements) between databases as CSV files.

Used to stand up a server without re-running the downloads and OCR: export on a machine that
has the data, import into a freshly migrated, empty database.
"""

import gzip
from pathlib import Path

# Order matters for foreign keys. Forecasts and alerts are not copied; they are regenerated.
TABLES = ("nodes", "reaches", "stations", "sources", "timeseries", "observations")
SEQUENCES = {"nodes": "id", "reaches": "id", "sources": "id", "observations": "id"}


def export_seed(conn, out_dir: Path, log=print) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        path = out_dir / f"{table}.csv.gz"
        with gzip.open(path, "wb") as fh, conn.cursor() as cur:
            with cur.copy(
                f"COPY (SELECT * FROM {table}) TO STDOUT WITH (FORMAT csv, HEADER)"
            ) as copy:
                for chunk in copy:
                    fh.write(chunk)
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        log(f"{table}: {n} rows -> {path.name} ({path.stat().st_size / 1e6:.1f} MB)")


def import_seed(conn, in_dir: Path, log=print) -> None:
    """Load the CSVs into an empty database. Refuses to run if data is already present."""
    if conn.execute("SELECT count(*) FROM nodes").fetchone()[0]:
        raise RuntimeError("database already has data; seed import only fills an empty one")
    for table in TABLES:
        path = in_dir / f"{table}.csv.gz"
        with gzip.open(path, "rb") as fh, conn.cursor() as cur:
            header = fh.readline().decode().strip()
            with cur.copy(f"COPY {table} ({header}) FROM STDIN WITH (FORMAT csv)") as copy:
                while chunk := fh.read(1 << 20):
                    copy.write(chunk)
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        log(f"{table}: {n} rows loaded")
    for table, column in SEQUENCES.items():
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
            f"(SELECT coalesce(max({column}), 1) FROM {table}))"
        )
    conn.commit()
