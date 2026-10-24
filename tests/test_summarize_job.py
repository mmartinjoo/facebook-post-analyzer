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
from app.summary.job import mark_stale, run_summarize_job
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
    assert (kontigo["sentence_input_tokens"], kontigo["sentence_output_tokens"]) == (200, 30)
    assert kontigo["comments_input_tokens"] is None
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
    assert (row["comments_input_tokens"], row["comments_output_tokens"]) == (100, 50)
    assert (row["sentence_input_tokens"], row["sentence_output_tokens"]) == (200, 30)

    assert slug_run()["clustered"] == 0
    assert client.messages.parse.call_count == 1
    row = rows(db)[0]
    assert row["comments_computed_at"] == first_computed and row["comments_model"] == "claude-sonnet-5-5"
    assert (row["comments_input_tokens"], row["comments_output_tokens"]) == (100, 50)  # kept, not re-clustered
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
    budget.remaining.side_effect = [200, 200, 0]  # two targets, then less than the reserve is left
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


MONTHS = [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]


def stale_count(db):
    return db.execute("select count(*) as n from monthly_summaries where stale").fetchone()["n"]


def test_mark_stale_creates_placeholders_and_flags_existing(db, settings, client):
    assert mark_stale(db, M) == 6
    assert stale_count(db) == 6
    run(db, settings, client)
    assert stale_count(db) == 0
    assert mark_stale(db, M, page_id=kontigo_id(db)) == 2  # the page + the all-pages row
    flagged = db.execute("select page_id from monthly_summaries where stale order by page_id nulls last").fetchall()
    assert [r["page_id"] for r in flagged] == [kontigo_id(db), None]


def test_drain_processes_all_stale_months(db, settings, client, no_continue):
    for m in MONTHS:
        seed(db, page_slug="kontigo", month=m, posts=[{"reactions_count": 3}])
        mark_stale(db, m)
    result = run_summarize_job(db, settings, client, TimeBudget(1000))
    assert result["mode"] == "drain"
    assert (result["processed"], result["failed"], result["remaining"]) == (18, 0, 0)
    rs = rows(db)
    assert len(rs) == 18 and all(r["stale"] is False and r["summary_sentence"] for r in rs)
    assert {r["month"] for r in rs} == set(MONTHS)
    assert no_continue == []
    assert db.execute("select count(*) as n from job_locks").fetchone()["n"] == 0


def test_drain_keeps_stale_while_posts_pending(db, settings, client, no_continue):
    seed(db, page_slug="kontigo", month=M, posts=[{}, {"analysis_status": "pending"}])
    mark_stale(db, M)
    result = run_summarize_job(db, settings, client, TimeBudget(1000))
    assert result["processed"] == 4 and result["remaining"] == 0  # Kontigo + all-pages skipped, not summarised
    still = db.execute(
        "select page_id, summary_sentence, post_count from monthly_summaries where stale order by page_id nulls last"
    ).fetchall()
    assert [r["page_id"] for r in still] == [kontigo_id(db), None]
    assert all(r["summary_sentence"] is None and r["post_count"] is None for r in still)  # still placeholders
    client.messages.create.assert_not_called()  # the 4 summarised pages had no posts -> no Haiku call either
    assert no_continue == []  # already looked at in this run -> left for the next drain, no self-loop

    db.execute("update posts set analysis_status = 'done' where analysis_status = 'pending'")
    db.commit()
    again = run_summarize_job(db, settings, client, TimeBudget(1000))
    assert again["processed"] == 2 and stale_count(db) == 0
    assert client.messages.create.call_count == 2


def test_drain_does_not_continue_without_progress(db, settings, client, monkeypatch):
    seed(db, page_slug="kontigo", month=M, posts=[{"analysis_status": "pending"}])
    mark_stale(db, M, page_id=kontigo_id(db))  # Kontigo + all-pages, both pending-blocked
    calls = []
    monkeypatch.setattr("app.summary.job.continue_job", lambda *a, **k: calls.append(a))
    result = run_summarize_job(db, settings, client, TimeBudget(1000))
    assert result["processed"] == 0 and result["remaining"] == 0
    assert calls == []
    client.messages.create.assert_not_called()
    assert stale_count(db) == 2
    assert db.execute("select count(*) as n from job_locks").fetchone()["n"] == 0


def test_drain_failed_target_stays_stale(db, settings, client):
    [pid] = seed(db, page_slug="kontigo", month=M, posts=[{}])
    add_comments(db, pid, ["Mennyibe kerül?", "Mennyi az ár?", "Drága"])
    mark_stale(db, M)
    normal = client.messages.parse.return_value
    client.messages.parse.side_effect = [RuntimeError("boom"), normal]
    result = run_summarize_job(db, settings, client, TimeBudget(1000))
    assert result["failed"] == 1 and result["processed"] == 5 and result["remaining"] == 0
    assert stale_count(db) == 1
    client.messages.parse.side_effect = None
    assert run_summarize_job(db, settings, client, TimeBudget(1000))["processed"] == 1
    assert stale_count(db) == 0


def test_drain_budget_expired_continues_without_month(db, settings, client, monkeypatch):
    # progress made, rows left -> one param-less continuation, lock already released
    mark_stale(db, M)
    lock_held, calls = [], []

    def fake_continue(*a, **k):
        lock_held.append(db.execute("select count(*) as n from job_locks").fetchone()["n"])
        calls.append((a, k))

    monkeypatch.setattr("app.summary.job.continue_job", fake_continue)
    budget = MagicMock()
    budget.remaining.side_effect = [1000, 0]  # one target, then the budget is below the reserve
    result = run_summarize_job(db, settings, client, budget)
    assert result["processed"] == 1 and result["remaining"] == 5
    assert lock_held == [0]
    (args, kwargs), = calls
    assert args[1:] == ("/api/jobs/summarize",) and kwargs == {}


def test_route_without_month_drains(db, settings, client):
    mark_stale(db, M)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_anthropic_client] = lambda: client
    try:
        http = TestClient(app)
        h = {"X-Admin-Secret": "admin-s"}
        assert http.post("/api/jobs/summarize?page=kontigo", headers=h).status_code == 400
        r = http.post("/api/jobs/summarize", headers=h)
        assert r.status_code == 200 and r.json()["mode"] == "drain" and r.json()["processed"] == 6
    finally:
        app.dependency_overrides.clear()


def test_drain_reaches_newer_months_past_pending_head(db, settings, client, no_continue):
    # reviewer's livelock reproduction: every page pending in the oldest month, done posts later
    for slug in ("raktar24", "kontigo", "kontenertar", "vnv", "erdraktar"):
        seed(db, page_slug=slug, month=MONTHS[0], posts=[{"analysis_status": "pending"}])
    for m in MONTHS[1:]:
        seed(db, page_slug="kontigo", month=m, posts=[{}])
    for m in MONTHS:
        mark_stale(db, m)
    result = run_summarize_job(db, settings, client, TimeBudget(1000))
    assert result["processed"] == 12 and result["remaining"] == 0
    stale = db.execute("select distinct month from monthly_summaries where stale").fetchall()
    assert [r["month"] for r in stale] == [MONTHS[0]]
    assert client.messages.create.call_count == 4  # Kontigo + all-pages for Aug and Sep only
