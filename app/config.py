from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    apify_api_token: str = ""
    anthropic_api_key: str = ""
    database_url: str = ""
    cron_secret: str = ""
    apify_webhook_secret: str = ""
    admin_secret: str = ""
    app_base_url: str = ""
    job_time_budget_seconds: int = 200
    prompt_version: str = "v1"
    test_database_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
