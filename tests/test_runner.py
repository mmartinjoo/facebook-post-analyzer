import httpx

from app.jobs.runner import (
    JOB_ANALYZE,
    TimeBudget,
    acquire_lock,
    continue_job,
    release_lock,
)


def test_lock_is_exclusive_until_released(db):
    assert acquire_lock(db, JOB_ANALYZE, owner="a") is True
    assert acquire_lock(db, JOB_ANALYZE, owner="b") is False
    release_lock(db, JOB_ANALYZE, owner="a")
    assert acquire_lock(db, JOB_ANALYZE, owner="b") is True


def test_release_by_non_owner_keeps_lock(db):
    assert acquire_lock(db, JOB_ANALYZE, owner="a") is True
    release_lock(db, JOB_ANALYZE, owner="b")
    assert acquire_lock(db, JOB_ANALYZE, owner="c") is False


def test_expired_lock_can_be_taken(db):
    assert acquire_lock(db, JOB_ANALYZE, ttl_seconds=-1, owner="a") is True
    assert acquire_lock(db, JOB_ANALYZE, owner="b") is True


def test_continue_job_swallows_timeout(settings, monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert continue_job(settings, "/api/jobs/analyze") is None
    url, kwargs = calls[0]
    assert url == "http://testserver/api/jobs/analyze"
    assert kwargs["headers"]["X-Admin-Secret"] == "admin-s"


def test_continue_job_logs_other_errors(settings, monkeypatch, caplog):
    def fake_post(url, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert continue_job(settings, "/api/jobs/analyze") is None
    assert "boom" in caplog.text


def test_time_budget_expires():
    assert TimeBudget(0).expired() is True
    assert TimeBudget(100).expired() is False
