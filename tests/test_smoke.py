import psycopg
import pytest

from ganga.config import settings
from ganga.db import connect


def test_dsn_shape():
    assert settings.db_dsn.startswith("postgresql://")
    assert str(settings.db_port) in settings.db_dsn


@pytest.mark.db
def test_db_has_extensions():
    try:
        conn = connect(connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database container not running")
    with conn:
        rows = conn.execute("SELECT extname FROM pg_extension").fetchall()
    names = {r[0] for r in rows}
    assert {"timescaledb", "postgis"} <= names
