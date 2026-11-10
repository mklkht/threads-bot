"""Shared fixtures: settings with test values and a Store on a temporary SQLite file."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import Settings
from app.store import Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        threads_access_token="test-token",
        threads_api_base="https://graph.threads.net/v1.0",
        llm_provider="anthropic",
        llm_api_key="test-llm-key",
        llm_model_post="test-post-model",
        llm_model_classify="test-classify-model",
        account_persona="тестова персона",
        niche_topics="C# та .NET; Angular ;  ; DevOps",
        db_path=str(tmp_path / "bot.db"),
        dry_run=True,
    )


@pytest_asyncio.fixture
async def tmp_store(tmp_path: Path) -> AsyncIterator[Store]:
    store = Store(str(tmp_path / "test.db"))
    await store.init()
    try:
        yield store
    finally:
        await store.close()
