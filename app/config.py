from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

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
    # Vercel "Protection Bypass for Automation" secret; empty = not sent
    vercel_protection_bypass: str = ""

    @field_validator("app_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
