import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api.index import app
from app.analysis.extractor import get_anthropic_client
from app.config import get_settings
from app.db import get_db
from app.jobs.runner import TimeBudget
from app.summary.comments_llm import CommentInsights, Insight
from app.summary.job import run_summarize_job
from tests.fixtures.seed_month import seed

M = date(2026, 9, 1)


@pytest.fixture(autouse=True)
def no_continue(monkeypatch):
    calls = []
    monkeypatch.setattr("app.summary.job.continue_job", lambda *a, **k: calls.append((a, k)))
    return calls


@pytest.fixture
def client():
    c = MagicMock()
    c.messages.parse.return_value = MagicMock(
        parsed_output=CommentInsights(
            top_questions=[Insight(text="Mennyibe kerül?", count=2, example="Mennyi az ár?")],
            top_objections=[Insight(text="Túl drága", count=1, example="Drága")],
        ),
        usage=MagicMock(input_tokens=100, output_tokens=50),
    )
    c.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Kontigo 2026 szeptember hónapban a magánszemélyeket célozta.")],
        usage=MagicMock(input_tokens=200, output_tokens=30),
    )
    return c


def add_comments(conn, post_id, texts, scraped_at=None, author_is_page=False):
    for t in texts:
        conn.execute(
            """insert into comments (post_id, fb_comment_id, text, author_is_page, posted_at, scraped_at)
               values (%s, %s, %s, %s, %s, coalesce(%s, now()))""",
            (post_id, f"c-{uuid.uuid4()}", t, author_is_page, datetime(2026, 9, 20, tzinfo=timezone.utc), scraped_at),
        )
    conn.commit()


def run(db, settings, client, **kw):
    return run_summarize_job(db, settings, client, TimeBudget(200), month=M, **kw)


def rows(db):
    return db.execute("select * from monthly_summaries order by page_id nulls last").fetchall()


def kontigo_id(db):
    return db.execute("select id from pages where slug = 'kontigo'").fetchone()["id"]


def test_creates_rows_for_each_page_and_all(db, settings, client):
    seed(db, page_slug="kontigo", month=M, posts=[{"reactions_count": 5}])
    result = run(db, settings, client)
    assert result == {
        "month": "2026-09", "processed": 6, "remaining": 0, "clustered": 0, "failed": 0,
        "tokens": {"haiku": {"input": 400, "output": 60}, "sonnet": {"input": 0, "output": 0}},
    }
    rs = rows(db)
    assert len(rs) == 6
    assert sum(r["page_id"] is None for r in rs) == 1
    assert all(r["stale"] is False and r["prompt_version"] == "v1" for r in rs)
    assert all(r["summary_model"] == "claude-haiku-5-5" and r["stats_computed_at"] for r in rs)
    kontigo = next(r for r in rs if r["page_id"] == kontigo_id(db))
    assert kontigo["post_count"] == 1 and kontigo["summary_sentence"].startswith("Kontigo")
    empty = next(r for r in rs if r["post_count"] == 0)
    assert "nem volt poszt 2026 szeptember" in empty["summary_sentence"]
    assert client.messages.create.call_count == 2  # Kontigo + all-pages; zero-post pages skip the LLM
    assert db.execute("select count(*) as n from job_locks where job_name = 'summarize'").fetchone()["n"] == 0


def test_comments_clustering_only_when_new_comments(db, settings, client):
    [pid] = seed(db, page_slug="kontigo", month=M, posts=[{}])
    add_comments(db, pid, ["Mennyibe kerül?", "Mennyi az ár?", "Drága"])
    slug_run = lambda: run(db, settings, client, page_slug="kontigo")
    first = slug_run()
    assert first["clustered"] == 1
    assert first["tokens"]["sonnet"] == {"input": 100, "output": 50}
    assert client.messages.parse.call_count == 1
    row = rows(db)[0]
    assert row["comments_model"] == "claude-sonnet-5-5" and row["comments_computed_at"] is not None
    assert row["top_questions"][0]["text"] == "Mennyibe kerül?"
    first_computed = row["comments_computed_at"]

    assert slug_run()["clustered"] == 0
    assert client.messages.parse.call_count == 1
    row = rows(db)[0]
    assert row["comments_computed_at"] == first_computed and row["comments_model"] == "claude-sonnet-5-5"
    assert row["top_questions"][0]["text"] == "Mennyibe kerül?"

    add_comments(db, pid, ["Hol van?"], scraped_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert slug_run()["clustered"] == 1
    assert client.messages.parse.call_count == 2


def test_stats_only_skips_sonnet(db, settings, client):
    [pid] = seed(db, page_slug="kontigo", month=M, posts=[{}])
    add_comments(db, pid, ["a?", "b?", "c?"])
    result = run(db, settings, client, stats_only=True)
    assert result["clustered"] == 0 and result["processed"] == 6
    client.messages.parse.assert_not_called()
    assert all(r["top_questions"] == [] and r["comments_computed_at"] is None for r in rows(db))


def test_upsert_overwrites_existing(db, settings, client):
    [pid] = seed(db, page_slug="kontigo", month=M, posts=[{"reactions_count": 5}])
    run(db, settings, client)
    db.execute("update posts set reactions_count = 42 where id = %s", (pid,))
    db.execute("update monthly_summaries set stale = true")
    db.commit()
    run(db, settings, client)
    rs = rows(db)
    assert len(rs) == 6
    assert all(r["best_post_engagement"] == 42 for r in rs if r["post_count"] == 1)
    assert sum(1 for r in rs if r["post_count"] == 1) == 2  # Kontigo + the all-pages (NULL) row
    assert all(r["stale"] is False for r in rs)


def test_budget_expiry_releases_lock_then_continues(db, settings, client, monkeypatch):
    budget = MagicMock()
    budget.expired.side_effect = [False, False, True]
    lock_held, calls = [], []

    def fake_continue(*a, **k):
        lock_held.append(db.execute("select count(*) as n from job_locks").fetchone()["n"])
        calls.append((a, k))

    monkeypatch.setattr("app.summary.job.continue_job", fake_continue)
    result = run_summarize_job(db, settings, client, budget, month=M, stats_only=True)
    assert result["processed"] == 2 and result["remaining"] == 4
    assert lock_held == [0]
    (args, _), = calls
    assert args[1:] == ("/api/jobs/summarize", {"month": "2026-09", "stats_only": "1"})


def test_route_rejects_bad_month(db, settings, client):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_anthropic_client] = lambda: client
    try:
        http = TestClient(app)
        h = {"X-Admin-Secret": "admin-s"}
        assert http.post("/api/jobs/summarize?month=2026-13", headers=h).status_code == 400
        r = http.post("/api/jobs/summarize?month=2026-09&page=kontigo&stats_only=1", headers=h)
        assert r.status_code == 200 and r.json()["processed"] == 1
        assert http.post("/api/jobs/summarize?month=2026-09&page=nope", headers=h).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_failing_target_does_not_abort_run(db, settings, client):
    [pid] = seed(db, page_slug="kontigo", month=M, posts=[{}])
    add_comments(db, pid, ["Mennyibe kerül?", "Mennyi az ár?", "Drága"])
    normal = client.messages.parse.return_value
    client.messages.parse.side_effect = [RuntimeError("boom"), normal]  # Kontigo and all-pages both cluster
    result = run(db, settings, client)
    assert result["failed"] == 1 and result["processed"] == 5 and result["remaining"] == 0
    assert result["clustered"] == 1
    rs = rows(db)
    assert len(rs) == 5  # the failed target's row was rolled back, the other five were written
    assert sum(1 for r in rs if r["comments_computed_at"] is not None) == 1
    assert db.execute("select count(*) as n from job_locks").fetchone()["n"] == 0

    client.messages.parse.side_effect = None
    retry = run(db, settings, client)
    assert retry["failed"] == 0 and retry["clustered"] == 1  # only the failed target re-clusters
    assert len(rows(db)) == 6
