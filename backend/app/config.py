from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_secret(name: str, default: str | None = None) -> str | None:
    """Docker secrets convention: prefer {name}_FILE (path to a file containing the
    value) over {name} (the value directly), so real deployments never need the
    secret sitting in plaintext in the process environment."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        with open(file_path, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get(name, default)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "ecommerce"

    agent_db_user: str = "agent_ro"
    refresher_db_user: str = "refresher_rw"

    llm_provider: str = "mock"  # "mock" | "gemini"
    gemini_model: str = "gemini-2.0-flash"

    cors_allow_origins: list[str] = ["http://localhost:3000"]

    session_ttl_seconds: int = 1800
    session_max_turns: int = 6

    rate_limit_capacity: int = 10
    rate_limit_refill_per_minute: int = 10

    max_generation_retries: int = 2

    refresh_interval_minutes: int = 60

    @property
    def agent_db_password(self) -> str:
        pw = _resolve_secret("POSTGRES_AGENT_PASSWORD")
        if not pw:
            raise RuntimeError("POSTGRES_AGENT_PASSWORD(_FILE) is not set")
        return pw

    @property
    def refresher_db_password(self) -> str:
        pw = _resolve_secret("POSTGRES_REFRESHER_PASSWORD")
        if not pw:
            raise RuntimeError("POSTGRES_REFRESHER_PASSWORD(_FILE) is not set")
        return pw

    @property
    def gemini_api_key(self) -> str | None:
        return _resolve_secret("GEMINI_API_KEY")

    def agent_dsn(self) -> str:
        return (
            f"postgresql://{self.agent_db_user}:{self.agent_db_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def refresher_dsn(self) -> str:
        return (
            f"postgresql://{self.refresher_db_user}:{self.refresher_db_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
