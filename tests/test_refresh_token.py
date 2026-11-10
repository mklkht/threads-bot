"""Tests for adopting the configured Threads token at startup."""

from __future__ import annotations

from typing import Any

import pytest

from app.jobs.refresh_token import bootstrap_token, verify_account
from app.store import Store
from app.threads_api import TokenInvalidError


class _FakeThreads:
    def __init__(self, store: Store, steps: list[Any]) -> None:
        self.store = store
        self.steps = list(steps)
        self.seen: list[str] = []

    async def get_me(self) -> dict[str, Any]:
        info = await self.store.get_token_info()
        self.seen.append("" if info is None else str(info["access_token"]))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.mark.asyncio
async def test_bootstrap_keeps_a_refreshed_token(tmp_store: Store) -> None:
    await tmp_store.save_token("refreshed", "2026-01-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00")
    await bootstrap_token(tmp_store, "from-env")
    info = await tmp_store.get_token_info()
    assert info is not None
    assert info["access_token"] == "refreshed"
    assert info["refreshed_at"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_verify_replaces_placeholder_before_the_api_call(tmp_store: Store) -> None:
    await tmp_store.save_token("REPLACE_ME", "2026-01-01T00:00:00+00:00", None)
    threads = _FakeThreads(tmp_store, [{"id": "1", "username": "mrsmithjr123"}])

    me = await verify_account(tmp_store, threads, "real-token")

    assert me["username"] == "mrsmithjr123"
    assert threads.seen == ["real-token"]
    info = await tmp_store.get_token_info()
    assert info is not None
    assert info["access_token"] == "real-token"


@pytest.mark.asyncio
async def test_verify_retries_with_settings_when_stored_token_is_rejected(tmp_store: Store) -> None:
    await tmp_store.save_token("stale", "2026-01-01T00:00:00+00:00", None)
    threads = _FakeThreads(
        tmp_store,
        [
            TokenInvalidError(400, 190, None, "invalid"),
            {"id": "1", "username": "mrsmithjr123"},
        ],
    )

    me = await verify_account(tmp_store, threads, "real-token")

    assert me["id"] == "1"
    assert threads.seen == ["stale", "real-token"]
    info = await tmp_store.get_token_info()
    assert info is not None
    assert info["access_token"] == "real-token"


@pytest.mark.asyncio
async def test_verify_does_not_replace_a_rejected_token_with_a_placeholder(tmp_store: Store) -> None:
    await tmp_store.save_token("stale", "2026-01-01T00:00:00+00:00", None)
    threads = _FakeThreads(tmp_store, [TokenInvalidError(400, 190, None, "invalid")])

    with pytest.raises(TokenInvalidError):
        await verify_account(tmp_store, threads, "REPLACE_ME")

    assert threads.seen == ["stale"]
    info = await tmp_store.get_token_info()
    assert info is not None
    assert info["access_token"] == "stale"
