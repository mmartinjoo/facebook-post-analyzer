from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.jobs.backfill as backfill_module
from api.index import app
from app.apify_runs import get_apify_client
from app.config import get_settings
from app.db import get_db
from app.timeutil import previous_month

AUTH = {"Authorization": "Bearer cron-s"}


@pytest.fixture
def calls(monkeypatch):
    collected = []
    monkeypatch.setattr(backfill_module, "continue_job", lambda settings, path, params=None: collected.append((path, params)))
    return collected


@pytest.fixture
def apify():
    m = MagicMock()
    m.actor.return_value.start.return_value = MagicMock(id="run1", default_dataset_id="ds1")
    return m


@pytest.fixture
def client(settings, db, calls, apify):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_apify_client] = lambda: apify
    yield TestClient(app)
    app.dependency_overrides.clear()


def _add_post(db, n, posted_at):
    page_id = db.execute("SELECT id FROM pages ORDER BY id LIMIT 1").fetchone()["id"]
    db.execute(
        "INSERT INTO posts (page_id, fb_post_id, url, posted_at) VALUES (%s, %s, %s, %s)",
        (page_id, f"p{n}", f"https://www.facebook.com/x/posts/{n}", posted_at),
    )
    db.commit()


def test_monthly_starts_comments_run_for_previous_month(client, db, calls, apify):
    prev = previous_month()
    mid = datetime(prev.year, prev.month, 15, 12, tzinfo=timezone.utc)
    _add_post(db, 1, mid)
    _add_post(db, 2, mid.replace(day=16))
    _add_post(db, 3, datetime.now(timezone.utc))
    r = client.get("/api/cron/monthly", headers=AUTH)
    assert r.status_code == 200
    start_kwargs = apify.actor.return_value.start.call_args.kwargs
    assert len(start_kwargs["run_input"]["startUrls"]) == 2
    row = db.execute("SELECT kind, params FROM scrape_runs WHERE apify_run_id = 'run1'").fetchone()
    assert row["kind"] == "monthly_comments"
    assert row["params"]["month"] == prev.strftime("%Y-%m")
    assert calls == []


def test_monthly_without_posts_goes_straight_to_summarize(client, db, calls, apify):
    r = client.get("/api/cron/monthly", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["run_id"] is None
    assert calls == [("/api/jobs/summarize", {"month": previous_month().strftime("%Y-%m")})]
    apify.actor.return_value.start.assert_not_called()
