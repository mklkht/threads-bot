"""Tests for the Threads API client: retries, error mapping, polling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx

from app.store import Store
from app.threads_api import ThreadsApiError, ThreadsClient, TokenInvalidError

API_BASE = "https://graph.threads.net/v1.0"


async def _no_sleep(_: float) -> None:
    return None


@pytest_asyncio.fixture
async def client(tmp_store: Store) -> AsyncIterator[ThreadsClient]:
    await tmp_store.save_token("test-token", datetime.now(timezone.utc).isoformat(), None)
    c = ThreadsClient(tmp_store, API_BASE, sleep=_no_sleep)
    try:
        yield c
    finally:
        await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_me_returns_json(client: ThreadsClient) -> None:
    respx.route(method="GET", path="/v1.0/me").mock(
        return_value=httpx.Response(200, json={"id": "123", "username": "tester"})
    )
    me = await client.get_me()
    assert me == {"id": "123", "username": "tester"}


@pytest.mark.asyncio
@respx.mock
async def test_retries_on_429_then_succeeds(client: ThreadsClient) -> None:
    route = respx.route(method="GET", path="/v1.0/me").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "rate", "code": 4}}),
            httpx.Response(429, json={"error": {"message": "rate", "code": 4}}),
            httpx.Response(200, json={"id": "1", "username": "u"}),
        ]
    )
    me = await client.get_me()
    assert me["id"] == "1"
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_oauth_error_raises_token_invalid(client: ThreadsClient) -> None:
    respx.route(method="GET", path="/v1.0/me").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "Invalid token", "type": "OAuthException", "code": 190}},
        )
    )
    with pytest.raises(TokenInvalidError):
        await client.get_me()


@pytest.mark.asyncio
@respx.mock
async def test_client_error_is_not_retried(client: ThreadsClient) -> None:
    route = respx.route(method="GET", path="/v1.0/me").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad", "code": 100}})
    )
    with pytest.raises(ThreadsApiError) as info:
        await client.get_me()
    assert info.value.code == 100
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_wait_until_finished_polls_until_finished(client: ThreadsClient) -> None:
    route = respx.route(method="GET", path="/v1.0/cont_1").mock(
        side_effect=[
            httpx.Response(200, json={"status": "IN_PROGRESS"}),
            httpx.Response(200, json={"status": "IN_PROGRESS"}),
            httpx.Response(200, json={"status": "FINISHED"}),
        ]
    )
    await client.wait_until_finished("cont_1", timeout_sec=60)
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_wait_until_finished_raises_on_error_status(client: ThreadsClient) -> None:
    respx.route(method="GET", path="/v1.0/cont_2").mock(
        return_value=httpx.Response(200, json={"status": "ERROR", "error_message": "media failed"})
    )
    with pytest.raises(ThreadsApiError):
        await client.wait_until_finished("cont_2", timeout_sec=60)


@pytest.mark.asyncio
@respx.mock
async def test_get_conversation_follows_pagination(client: ThreadsClient) -> None:
    page1: dict[str, Any] = {
        "data": [{"id": "r1"}],
        "paging": {"cursors": {"after": "CUR1"}, "next": "https://graph.threads.net/next"},
    }
    page2: dict[str, Any] = {"data": [{"id": "r2"}], "paging": {"cursors": {"after": "CUR2"}}}
    route = respx.route(method="GET", path="/v1.0/post_9/conversation").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    items = await client.get_conversation("post_9")
    assert [i["id"] for i in items] == ["r1", "r2"]
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_token_is_read_from_store_per_request(client: ThreadsClient, tmp_store: Store) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["access_token"])
        return httpx.Response(200, json={"id": "1", "username": "u"})

    respx.route(method="GET", path="/v1.0/me").mock(side_effect=handler)
    await client.get_me()
    await tmp_store.save_token("rotated-token", datetime.now(timezone.utc).isoformat(), None)
    await client.get_me()
    assert seen == ["test-token", "rotated-token"]
