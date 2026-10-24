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


def test_unknown_run_404(client, db):
    r = client.post("/api/webhooks/apify?token=hook-s", json=_payload())
    assert r.status_code == 404
    assert _post_count(db) == 0


def test_bad_token_401(client, db):
    _insert_run(db)
    r = client.post("/api/webhooks/apify?token=nope", json=_payload())
    assert r.status_code == 401
    assert _run(db)["status"] == "started"
