import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.jobs.webhook as webhook_module
from api.index import app
from app.apify_runs import get_apify_client
from app.config import get_settings
from app.db import get_db

ITEMS = json.loads((Path(__file__).parent / "fixtures" / "apify_posts_sample.json").read_text())


@pytest.fixture
def continued(monkeypatch):
    calls = []
    monkeypatch.setattr(webhook_module, "continue_job", lambda *a, **k: calls.append(a))
    return calls


@pytest.fixture
def client(settings, db, continued):
    apify = MagicMock()
    apify.dataset.return_value.iterate_items.return_value = list(ITEMS)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_apify_client] = lambda: apify
    yield TestClient(app)
    app.dependency_overrides.clear()


def _insert_run(db, status="started"):
    db.execute(
        "INSERT INTO scrape_runs (apify_run_id, actor, kind, status) VALUES ('run1', 'posts', 'daily', %s)",
        (status,),
    )
    db.commit()


def _payload(event="ACTOR.RUN.SUCCEEDED"):
    return {"runId": "run1", "status": "SUCCEEDED", "datasetId": "ds1", "eventType": event}


def _post_count(db):
    return db.execute("SELECT count(*) AS n FROM posts").fetchone()["n"]


def _run(db):
    return db.execute("SELECT * FROM scrape_runs WHERE apify_run_id = 'run1'").fetchone()


def test_success_posts_run_ingests_and_continues(client, db, continued):
    _insert_run(db)
    r = client.post("/api/webhooks/apify?token=hook-s", json=_payload())
    assert r.status_code == 200
    assert _post_count(db) == 3
    run = _run(db)
    assert run["status"] == "processed"
    assert run["items_count"] == 3
    assert continued[0][1] == "/api/jobs/analyze"


def test_failed_event_marks_run_failed(client, db, continued):
    _insert_run(db)
    r = client.post("/api/webhooks/apify?token=hook-s", json=_payload("ACTOR.RUN.FAILED"))
    assert r.status_code == 200
    run = _run(db)
    assert run["status"] == "failed"
    assert run["error"] == "ACTOR.RUN.FAILED"
    assert _post_count(db) == 0
    assert continued == []


def test_duplicate_delivery_is_noop(client, db, continued):
    _insert_run(db)
    client.post("/api/webhooks/apify?token=hook-s", json=_payload())
    before = _post_count(db)
    continued.clear()
    r = client.post("/api/webhooks/apify?token=hook-s", json=_payload())
    assert r.status_code == 200
    assert r.json() == {"skipped": True}
    assert _post_count(db) == before
    assert continued == []


def test_duplicate_delivery_while_processing_is_skipped(client, db, continued):
    _insert_run(db, status="processing")
    r = client.post("/api/webhooks/apify?token=hook-s", json=_payload())
    assert r.status_code == 200
    assert r.json() == {"skipped": True, "reason": "in_progress"}
    assert _post_count(db) == 0 and _run(db)["status"] == "processing"
    assert continued == []


def test_failed_run_can_be_redelivered(client, db, continued):
    _insert_run(db, status="failed")
    assert client.post("/api/webhooks/apify?token=hook-s", json=_payload()).status_code == 200
    assert _run(db)["status"] == "processed" and _post_count(db) == 3


def test_continuation_error_does_not_fail_processed_run(settings, db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("continuation down")

    monkeypatch.setattr(webhook_module, "continue_job", boom)
    _insert_run(db)
    apify = MagicMock()
    apify.dataset.return_value.iterate_items.return_value = list(ITEMS)
    with pytest.raises(RuntimeError):
        webhook_module.process_webhook(db, settings, apify, _payload())
    assert _run(db)["status"] == "processed" and _post_count(db) == 3


def test_comments_mark_stale_failure_marks_run_failed(settings, db, monkeypatch):
    monkeypatch.setattr(webhook_module, "continue_job", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(webhook_module, "mark_stale", boom)
    db.execute(
        "INSERT INTO scrape_runs (apify_run_id, actor, kind, params, status) "
        "VALUES ('run1', 'comments', 'monthly_comments', '{\"month\": \"2026-09\"}', 'started')"
    )
    db.commit()
    apify = MagicMock()
    apify.dataset.return_value.iterate_items.return_value = []
    with pytest.raises(RuntimeError):
        webhook_module.process_webhook(db, settings, apify, _payload())
    assert _run(db)["status"] == "failed"  # re-deliverable


def test_unknown_run_404(client, db):
    r = client.post("/api/webhooks/apify?token=hook-s", json=_payload())
    assert r.status_code == 404
    assert _post_count(db) == 0


def test_bad_token_401(client, db):
    _insert_run(db)
    r = client.post("/api/webhooks/apify?token=nope", json=_payload())
    assert r.status_code == 401
    assert _run(db)["status"] == "started"


def test_comments_run_ingests_and_continues_summarize(client, db, continued):
    comment_items = json.loads((Path(__file__).parent / "fixtures" / "apify_comments_sample.json").read_text())
    post = ITEMS[0]
    page_id = db.execute("SELECT id FROM pages WHERE slug = 'kontigo'").fetchone()["id"]
    db.execute(
        "INSERT INTO posts (page_id, fb_post_id, url, posted_at) VALUES (%s, 'p1', %s, now())",
        (page_id, post["url"]),
    )
    db.execute(
        "INSERT INTO scrape_runs (apify_run_id, actor, kind, params, status) "
        "VALUES ('run1', 'comments', 'monthly_comments', '{\"month\": \"2026-09\"}', 'started')"
    )
    db.commit()
    client.app.dependency_overrides[get_apify_client]().dataset.return_value.iterate_items.return_value = comment_items
    r = client.post("/api/webhooks/apify?token=hook-s", json=_payload())
    assert r.status_code == 200
    assert db.execute("SELECT count(*) AS n FROM comments").fetchone()["n"] == len(comment_items)
    assert _run(db)["status"] == "processed"
    assert [c[1:] for c in continued] == [("/api/jobs/summarize",)]  # one param-less drain call
    stale = db.execute("SELECT month, count(*) AS n FROM monthly_summaries WHERE stale GROUP BY month").fetchall()
    assert [(r["month"].isoformat(), r["n"]) for r in stale] == [("2026-09-01", 6)]


def test_backfill_comments_run_marks_each_month_and_drains_once(client, db, continued):
    db.execute(
        "INSERT INTO scrape_runs (apify_run_id, actor, kind, params, status) "
        "VALUES ('run1', 'comments', 'backfill_comments', '{\"from\": \"2026-07\", \"to\": \"2026-09\"}', 'started')"
    )
    db.commit()
    client.app.dependency_overrides[get_apify_client]().dataset.return_value.iterate_items.return_value = []
    assert client.post("/api/webhooks/apify?token=hook-s", json=_payload()).status_code == 200
    assert [c[1:] for c in continued] == [("/api/jobs/summarize",)]
    stale = db.execute("SELECT DISTINCT month FROM monthly_summaries WHERE stale ORDER BY month").fetchall()
    assert [r["month"].isoformat() for r in stale] == ["2026-07-01", "2026-08-01", "2026-09-01"]


@pytest.mark.parametrize("with_valid", [True, False])
def test_comments_run_tolerates_error_items(client, db, continued, with_valid):
    comment_items = json.loads((Path(__file__).parent / "fixtures" / "apify_comments_sample.json").read_text())
    post = ITEMS[0]
    page_id = db.execute("SELECT id FROM pages WHERE slug = 'kontigo'").fetchone()["id"]
    db.execute(
        "INSERT INTO posts (page_id, fb_post_id, url, posted_at) VALUES (%s, 'p1', %s, now())",
        (page_id, post["url"]),
    )
    db.execute(
        "INSERT INTO scrape_runs (apify_run_id, actor, kind, params, status) "
        "VALUES ('run1', 'comments', 'monthly_comments', '{\"month\": \"2026-09\"}', 'started')"
    )
    db.commit()
    error_item = {
        "error": "no_data",
        "errorDescription": "No public data available without Facebook login or access rights",
        "inputUrl": post["url"],
    }
    items = ([error_item] + comment_items) if with_valid else [error_item]
    client.app.dependency_overrides[get_apify_client]().dataset.return_value.iterate_items.return_value = items
    r = client.post("/api/webhooks/apify?token=hook-s", json=_payload())
    assert r.status_code == 200
    run = _run(db)
    assert run["status"] == "processed"
    assert "items skipped" in run["error"]
    expected = len(comment_items) if with_valid else 0
    assert db.execute("SELECT count(*) AS n FROM comments").fetchone()["n"] == expected
    assert [c[1:] for c in continued] == [("/api/jobs/summarize",)]
