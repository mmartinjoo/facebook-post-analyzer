from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import app.analysis.job as job_module
from app.analysis.job import run_analyze_job
from app.jobs.runner import JOB_ANALYZE, TimeBudget, acquire_lock, release_lock
from tests.test_extractor import make_analysis, make_response


@pytest.fixture
def continued(monkeypatch):
    calls = []
    monkeypatch.setattr(job_module, "continue_job", lambda *a, **k: calls.append(a))
    return calls


def _add_posts(db, n):
    page_id = db.execute("SELECT id FROM pages ORDER BY id LIMIT 1").fetchone()["id"]
    for i in range(n):
        db.execute(
            "INSERT INTO posts (page_id, fb_post_id, text, posted_at, analysis_status) "
            "VALUES (%s, %s, 'szöveg', %s, 'pending')",
            (page_id, f"fb{i}", datetime(2026, 1, 1 + i, tzinfo=timezone.utc)),
        )
    db.commit()


def _client(**kw):
    c = MagicMock()
    c.messages.parse.return_value = make_response(make_analysis())
    for k, v in kw.items():
        setattr(c.messages.parse, k, v)
    return c


def test_processes_pending_and_marks_done(db, settings, continued):
    _add_posts(db, 2)
    res = run_analyze_job(db, settings, _client(), TimeBudget(60), prompt_version="v1")
    assert res == {"processed": 2, "failed": 0, "remaining": 0}
    assert db.execute("SELECT count(*) AS n FROM post_analyses WHERE prompt_version = 'v1'").fetchone()["n"] == 2
    assert {r["analysis_status"] for r in db.execute("SELECT analysis_status FROM posts").fetchall()} == {"done"}
    assert continued == []


def test_failure_increments_attempts_then_fails(db, settings, continued):
    _add_posts(db, 1)
    client = _client(side_effect=RuntimeError("boom"))
    results = [run_analyze_job(db, settings, client, TimeBudget(60), prompt_version="v1") for _ in range(3)]
    row = db.execute("SELECT analysis_status, analysis_attempts, analysis_error FROM posts").fetchone()
    assert row["analysis_status"] == "failed"
    assert row["analysis_attempts"] == 3
    assert row["analysis_error"] == "boom"
    assert results[0]["remaining"] == 0 and results[2]["failed"] == 1
    assert client.messages.parse.call_count == 3


def test_budget_expired_continues(db, settings, continued):
    _add_posts(db, 1)
    res = run_analyze_job(db, settings, _client(), TimeBudget(0), prompt_version="v1")
    assert res["processed"] == 0 and res["remaining"] == 1
    assert continued and continued[0][1] == "/api/jobs/analyze"


def test_lock_held_returns_locked(db, settings, continued):
    _add_posts(db, 1)
    assert acquire_lock(db, JOB_ANALYZE, owner="other")
    client = _client()
    assert run_analyze_job(db, settings, client, TimeBudget(60), prompt_version="v1") == {"locked": True}
    client.messages.parse.assert_not_called()


def test_lock_released_before_continuation(db, settings, monkeypatch):
    _add_posts(db, 1)
    seen = []

    def fake_continue(*a, **k):
        got = acquire_lock(db, JOB_ANALYZE, owner="next")
        seen.append(got)
        if got:
            release_lock(db, JOB_ANALYZE, "next")

    monkeypatch.setattr(job_module, "continue_job", fake_continue)
    run_analyze_job(db, settings, _client(), TimeBudget(0), prompt_version="v1")
    assert seen == [True]
