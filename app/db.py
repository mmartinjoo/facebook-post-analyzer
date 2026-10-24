import psycopg
from psycopg.rows import dict_row

from app.config import get_settings


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or get_settings().database_url, row_factory=dict_row, autocommit=False)
