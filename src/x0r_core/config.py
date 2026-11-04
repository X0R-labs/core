from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="X0R_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./x0r.db"

    provider: str = "mock"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    chat_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    request_timeout: float = 60.0

    http_tool_allowlist: str = ""
    operator_tool_allowlist: str = "knowledge_search,calculator"
    max_tool_calls_per_step: int = Field(default=8, ge=1, le=32)
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    max_http_response_bytes: int = Field(default=256 * 1024, ge=1024, le=8 * 1024 * 1024)
    http_tool_timeout: float = Field(default=10.0, gt=0, le=60)
    bootstrap_token: str = ""
    cors_origins: str = "*"

    @property
    def allowed_http_hosts(self) -> tuple[str, ...]:
        return tuple(h.strip().lower() for h in self.http_tool_allowlist.split(",") if h.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
