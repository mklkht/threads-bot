"""Application settings loaded from environment variables and .env."""

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # config.env (non-secret, committed to git) overrides .env (secrets, local-only)
    # because it is listed second — pydantic-settings reads env_file entries in order
    # and later files take priority.
    model_config = SettingsConfigDict(
        env_file=(".env", "config.env"), env_file_encoding="utf-8", extra="ignore"
    )

    # Threads API
    threads_access_token: str
    threads_api_base: str = "https://graph.threads.net/v1.0"

    # LLM
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    llm_api_key: str = ""
    llm_model_post: str
    llm_model_classify: str

    # Persona and topics
    account_persona: str
    niche_topics: str

    # Autopost
    post_cron: str = "30 9,19 * * *"
    post_jitter_sec: int = 1200
    post_max_chars: int = 400

    # Replies
    replies_interval_min: int = 10
    replies_posts_limit: int = 5
    replies_max_per_cycle: int = 10
    replies_max_per_user: int = 2
    reply_delay_min_sec: int = 15
    reply_delay_max_sec: int = 45
    reply_max_chars: int = 250

    # Token
    token_refresh_days: int = 25

    # Runtime
    dry_run: bool = False
    db_path: str = "/app/data/bot.db"
    log_level: str = "INFO"

    # Alerts
    alert_telegram_bot_token: str | None = None
    alert_telegram_chat_id: str | None = None

    @model_validator(mode="after")
    def _check_llm_key(self) -> "Settings":
        if self.llm_provider == "anthropic" and not self.llm_api_key:
            raise ValueError("LLM_API_KEY is required when LLM_PROVIDER=anthropic")
        return self

    def niche_topics_list(self) -> list[str]:
        """Split NICHE_TOPICS by ';', strip whitespace and drop empty items."""
        return [t.strip() for t in self.niche_topics.split(";") if t.strip()]
