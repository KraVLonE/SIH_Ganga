import psycopg

from ganga.config import settings


def connect(**kwargs) -> psycopg.Connection:
    return psycopg.connect(settings.db_dsn, **kwargs)
