import pytest
from pydantic import ValidationError

from app.config import Settings


def test_empty_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JOB_TIME_BUDGET_SECONDS", "")
    monkeypatch.setenv("PROMPT_VERSION", "")
    s = Settings(_env_file=None)
    assert s.job_time_budget_seconds == 200
    assert s.prompt_version == "v1"


def test_app_base_url_trailing_slash_stripped():
    s = Settings(_env_file=None, app_base_url="https://x.example.com/")
    assert s.app_base_url == "https://x.example.com"


def test_job_time_budget_below_reserve_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, job_time_budget_seconds=100)
    assert Settings(_env_file=None, job_time_budget_seconds=200).job_time_budget_seconds == 200
    assert Settings(_env_file=None, job_time_budget_seconds=140).job_time_budget_seconds == 140
