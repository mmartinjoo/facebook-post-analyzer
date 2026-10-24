from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.jobs.backfill as backfill_module
from api.index import app
from app.apify_runs import get_apify_client
from app.config import get_settings
from app.db import get_db
from app.timeutil import BUDAPEST, previous_month

AUTH = {"Authorization": "Bearer cron-s"}


@pytest.fixture
def calls(monkeypatch):
    collected = []
    monkeypatch.setattr(backfill_module, "continue_job", lambda settings, path, params=None: collected.append((path, params)))
    return collected


@pytest.fixture
def client(settings, db, calls):
    apify = MagicMock()
    apify.actor.return_value.start.return_value = MagicMock(id="run1", default_dataset_id="ds1")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_apify_client] = lambda: apify
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_daily_starts_run_and_chains(client, db, calls):
    today = datetime.now(BUDAPEST).date()
    db.execute("INSERT INTO monthly_summaries (page_id, month, stale) VALUES (NULL, '2025-03-01', true)")
    db.commit()
    r = client.get("/api/cron/daily", headers=AUTH)
    assert r.status_code == 200
    runs = db.execute("SELECT kind, params FROM scrape_runs").fetchall()
    assert len(runs) == 1 and runs[0]["kind"] == "daily"
    assert runs[0]["params"]["since"] == (today - timedelta(days=14)).isoformat()
    cur, prev = today.replace(day=1), previous_month(today)
    # one param-less drain call; no per-month summarize calls
    assert calls == [("/api/jobs/analyze", None), ("/api/jobs/summarize", None)]
    assert r.json()["summarize_months"] == [prev.strftime("%Y-%m"), cur.strftime("%Y-%m")]
    stale = db.execute("SELECT month, count(*) AS n FROM monthly_summaries WHERE stale GROUP BY month").fetchall()
    # the two recent months are queued for all 5 pages + the all-pages row; the old stale month stays queued
    assert {r["month"]: r["n"] for r in stale} == {prev: 6, cur: 6, datetime(2025, 3, 1).date(): 1}


def test_daily_requires_cron_secret(client):
    assert client.get("/api/cron/daily").status_code == 401
    assert client.get("/api/cron/daily", headers={"Authorization": "Bearer nope"}).status_code == 401
