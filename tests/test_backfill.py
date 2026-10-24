from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.jobs.backfill as backfill_module
from api.index import app
from app.apify_runs import get_apify_client
from app.config import get_settings
from app.db import get_db

ADMIN = {"X-Admin-Secret": "admin-s"}


@pytest.fixture
def calls(monkeypatch):
    collected = []
    monkeypatch.setattr(backfill_module, "continue_job", lambda settings, path, params=None: collected.append((path, params)))
    monkeypatch.setattr("api.index.continue_job", lambda settings, path, params=None: collected.append((path, params)))
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


def test_backfill_since(client, db):
    r = client.post("/api/jobs/backfill?since=2022-01-01", headers=ADMIN)
    assert r.status_code == 200
    run = db.execute("SELECT * FROM scrape_runs").fetchone()
    assert run["kind"] == "backfill"
    assert run["params"]["since"] == "2022-01-01"
    assert r.json() == {"run_id": run["id"]}


def _post(db, fb_id, status):
    page_id = db.execute("SELECT id FROM pages LIMIT 1").fetchone()["id"]
    return db.execute(
        "INSERT INTO posts (page_id, fb_post_id, analysis_status, analysis_attempts) VALUES (%s, %s, %s, 2) RETURNING id",
        (page_id, fb_id, status),
    ).fetchone()["id"]


def _analysis(db, post_id):
    db.execute(
        "INSERT INTO post_analyses (post_id, prompt_version, model, celkozonseg, ceges_szegmens, lakossagi_szegmens, "
        "use_case, fajdalompont, szezonalis_horog, ar_megjelenik, arhorgony, surgosseg, cta_tipus, konfidencia) "
        "VALUES (%s, 'v1', 'm', 'vallalkozas', 'nincs', 'nincs', 'u', 'f', 'nincs', false, 'nincs', 'nincs', 'nincs', 3)",
        (post_id,),
    )


def test_reanalyze_resets_posts_without_version(client, db, calls, settings):
    a, b, c = _post(db, "a", "done"), _post(db, "b", "done"), _post(db, "c", "skipped")
    _analysis(db, a)
    _analysis(db, b)
    db.execute("INSERT INTO monthly_summaries (page_id, month) VALUES (NULL, '2025-03-01')")
    db.commit()

    # version differs from the deployed PROMPT_VERSION: 400 and nothing changed
    r = client.post("/api/jobs/reanalyze?prompt_version=v2", headers=ADMIN)
    assert r.status_code == 400 and "set PROMPT_VERSION=v2" in r.json()["detail"]
    assert db.execute("SELECT count(*) AS n FROM posts WHERE analysis_status = 'pending'").fetchone()["n"] == 0
    assert calls == []

    # unknown prompt version (no prompt file): 400 and nothing changed
    settings.prompt_version = "v9"
    assert client.post("/api/jobs/reanalyze?prompt_version=v9", headers=ADMIN).status_code == 400
    assert db.execute("SELECT count(*) AS n FROM posts WHERE analysis_status = 'pending'").fetchone()["n"] == 0
    assert calls == []
    settings.prompt_version = "v1"

    # v1 already analyzed: nothing to reset
    r = client.post("/api/jobs/reanalyze?prompt_version=v1", headers=ADMIN)
    assert r.json() == {"reset": 0}
    calls.clear()

    # a version with a prompt file but no analyses
    from app.analysis.prompt import PROMPTS_DIR
    f = PROMPTS_DIR / "post_extraction_v2.md"
    f.write_text("x")
    settings.prompt_version = "v2"
    try:
        r = client.post("/api/jobs/reanalyze?prompt_version=v2", headers=ADMIN)
    finally:
        f.unlink()
    assert r.json() == {"reset": 2}
    statuses = {x["fb_post_id"]: x["analysis_status"] for x in db.execute("SELECT * FROM posts").fetchall()}
    assert statuses == {"a": "pending", "b": "pending", "c": "skipped"}
    assert db.execute("SELECT stale FROM monthly_summaries").fetchone()["stale"] is True
    assert calls == [("/api/jobs/analyze", None)]


def test_backfill_comments_one_run_per_month_with_posts(settings, db):
    from datetime import date

    from tests.fixtures.seed_month import seed

    seed(db, page_slug="kontigo", month=date(2026, 7, 1), posts=[{}])
    seed(db, page_slug="kontigo", month=date(2026, 9, 1), posts=[{}, {}])
    apify = MagicMock()
    apify.actor.return_value.start.side_effect = [
        MagicMock(id="r1", default_dataset_id="d1"),
        MagicMock(id="r2", default_dataset_id="d2"),
    ]
    ids = backfill_module.backfill_comments(
        db, settings, apify, from_month=date(2026, 6, 1), to_month=date(2026, 9, 1)
    )
    assert len(ids) == 2
    runs = db.execute("SELECT * FROM scrape_runs ORDER BY id").fetchall()
    assert [r["id"] for r in runs] == ids
    assert [r["params"]["month"] for r in runs] == ["2026-07", "2026-09"]
    assert all(r["kind"] == "backfill_comments" for r in runs)
    assert all(r["params"]["from"] == "2026-06" and r["params"]["to"] == "2026-09" for r in runs)


def test_summarize_range_marks_stale_and_drains_once(settings, db, calls):
    from datetime import date

    months = backfill_module.summarize_range(db, settings, from_month=date(2026, 1, 1), to_month=date(2026, 3, 1))
    assert months == ["2026-01", "2026-02", "2026-03"]
    assert calls == [("/api/jobs/summarize", None)]
    rows = db.execute("SELECT month, count(*) AS n FROM monthly_summaries WHERE stale GROUP BY month ORDER BY month").fetchall()
    assert [(r["month"], r["n"]) for r in rows] == [
        (date(2026, 1, 1), 6), (date(2026, 2, 1), 6), (date(2026, 3, 1), 6),
    ]


def test_range_routes(client, db, calls):
    h = ADMIN
    assert client.post("/api/jobs/backfill-comments?from=2026-09&to=2026-07", headers=h).status_code == 400
    assert client.post("/api/jobs/summarize-range?from=2026-x&to=2026-07", headers=h).status_code == 400
    assert client.post("/api/jobs/summarize-range?from=2026-01&to=2026-02").status_code == 401
    r = client.post("/api/jobs/summarize-range?from=2026-01&to=2026-02", headers=h)
    assert r.json() == {"months": ["2026-01", "2026-02"]}
    assert db.execute("SELECT count(*) AS n FROM monthly_summaries WHERE stale").fetchone()["n"] == 12
    r = client.post("/api/jobs/backfill-comments?from=2026-01&to=2026-02", headers=h)
    assert r.json() == {"run_ids": []}
