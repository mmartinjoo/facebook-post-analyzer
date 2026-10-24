from collections.abc import Iterator

import psycopg
from fastapi import Depends
from psycopg.rows import dict_row

from app.config import Settings, get_settings


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or get_settings().database_url, row_factory=dict_row, autocommit=False)


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[psycopg.Connection]:
    conn = connect(settings.database_url)
    try:
        yield conn
    finally:
        conn.close()
