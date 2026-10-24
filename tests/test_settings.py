from app.config import Settings


def test_empty_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JOB_TIME_BUDGET_SECONDS", "")
    monkeypatch.setenv("PROMPT_VERSION", "")
    s = Settings(_env_file=None)
    assert s.job_time_budget_seconds == 200
    assert s.prompt_version == "v1"
