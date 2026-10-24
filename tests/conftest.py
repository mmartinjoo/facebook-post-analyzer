import os
from urllib.parse import urlparse

import pytest

from app.config import Settings
from app.db import connect
from db.migrate import migrate

DEFAULT_TEST_DSN = "postgresql://postgres:postgres@localhost:55432/fpa_test"


def _test_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DSN)
    dbname = urlparse(dsn).path.lstrip("/")
    if not dbname.endswith("_test"):
        raise RuntimeError(
            f"Refusing to reset database {dbname!r}: TEST_DATABASE_URL must point to a database whose name ends in '_test'"
        )
    return dsn


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=_test_dsn(),
        test_database_url=_test_dsn(),
        cron_secret="cron-s",
        admin_secret="admin-s",
        apify_webhook_secret="hook-s",
        app_base_url="http://testserver",
    )


@pytest.fixture
def db(settings):
    dsn = settings.test_database_url
    with connect(dsn) as conn:
        conn.execute("drop schema public cascade")
        conn.execute("create schema public")
        conn.commit()
    migrate(dsn)
    conn = connect(dsn)
    yield conn
    conn.rollback()
    conn.close()
