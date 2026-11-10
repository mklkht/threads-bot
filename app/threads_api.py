"""Client for the Meta Threads Graph API with retries, token handling and structured logs."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.store import Store

logger = logging.getLogger("threads_api")

MAX_ATTEMPTS = 3
BACKOFF_SEC = (2, 4, 8)
OAUTH_ERROR_CODE = 190
POLL_INTERVAL_SEC = 2
MAX_PAGES = 50


class ThreadsApiError(Exception):
    def __init__(self, status_code: int | None, code: int | None, subcode: int | None, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.subcode = subcode
        self.message = message
        super().__init__(f"Threads API error status={status_code} code={code} subcode={subcode}: {message}")


class TokenInvalidError(ThreadsApiError):
    """Access token is rejected by the API (OAuthException code 190)."""


class ThreadsClient:
    def __init__(
        self,
        store: Store,
        api_base: str,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._api_base = api_base.rstrip("/")
        parts = urlsplit(self._api_base)
        self._root_url = f"{parts.scheme}://{parts.netloc}"
        self._sleep = sleep
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    # ---- low-level request ----

    async def _current_token(self) -> str:
        info = await self._store.get_token_info()
        if info is None or not info.get("access_token"):
            raise TokenInvalidError(None, OAUTH_ERROR_CODE, None, "no access token stored")
        return str(info["access_token"])

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        root: bool = False,
    ) -> dict[str, Any]:
        base = self._root_url if root else self._api_base
        url = f"{base}{path}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            token = await self._current_token()
            query = dict(params or {})
            query["access_token"] = token
            started = time.perf_counter()
            status: int | None = None
            try:
                resp = await self._http.request(method, url, params=query, data=data)
                status = resp.status_code
            except httpx.TransportError as exc:
                self._log(method, path, None, started)
                if attempt < MAX_ATTEMPTS:
                    await self._backoff(attempt)
                    continue
                raise ThreadsApiError(None, None, None, f"transport error: {type(exc).__name__}") from exc

            self._log(method, path, status, started)
            body = self._safe_json(resp)
            if 200 <= status < 300:
                return body

            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            code = err.get("code")
            subcode = err.get("error_subcode")
            message = str(err.get("message") or resp.reason_phrase or "unknown error")
            if code == OAUTH_ERROR_CODE:
                raise TokenInvalidError(status, code, subcode, message)
            retryable = status == 429 or status >= 500
            if retryable and attempt < MAX_ATTEMPTS:
                await self._backoff(attempt)
                continue
            raise ThreadsApiError(status, code, subcode, message)
        raise ThreadsApiError(None, None, None, "retries exhausted")  # pragma: no cover

    async def _backoff(self, attempt: int) -> None:
        delay = BACKOFF_SEC[min(attempt - 1, len(BACKOFF_SEC) - 1)] + random.uniform(0, 1)
        await self._sleep(delay)

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            parsed = resp.json()
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _log(method: str, path: str, status: int | None, started: float) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            json.dumps(
                {
                    "event": "threads_request",
                    "method": method,
                    "path": path,
                    "status": status,
                    "duration_ms": duration_ms,
                }
            )
        )

    # ---- public API ----

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me", params={"fields": "id,username"})

    async def get_my_threads(self, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", "/me/threads", params={"fields": "id,text,timestamp", "limit": limit}
        )
        return list(data.get("data", []))

    async def get_conversation(self, post_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "fields": "id,text,username,timestamp,replied_to,root_post,is_reply_owned_by_me,hide_status",
            "reverse": "false",
        }
        for _ in range(MAX_PAGES):
            data = await self._request("GET", f"/{post_id}/conversation", params=params)
            items.extend(data.get("data", []))
            paging = data.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after:
                break
            params = {**params, "after": after}
        return items

    async def search_popular_posts(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        """Top public posts for a keyword. Without threads_keyword_search this is only our own posts."""
        data = await self._request(
            "GET",
            "/keyword_search",
            params={
                "q": query,
                "search_type": "TOP",
                "search_mode": "KEYWORD",
                "fields": "id,text,permalink,timestamp,username,media_type",
                "limit": limit,
            },
        )
        return [dict(item) for item in data.get("data") or []]

    async def find_public_post(self, username: str, shortcode: str) -> dict[str, Any] | None:
        """Find a public post by username and the shortcode from its Threads URL."""
        params: dict[str, Any] = {
            "username": username,
            "fields": "id,text,permalink,shortcode,username",
            "limit": 50,
        }
        for _ in range(4):
            data = await self._request("GET", "/profile_posts", params=params)
            for item in data.get("data") or []:
                permalink = str(item.get("permalink") or "")
                if item.get("shortcode") == shortcode or f"/post/{shortcode}" in permalink:
                    return dict(item)
            after = ((data.get("paging") or {}).get("cursors") or {}).get("after")
            if not after:
                return None
            params = {**params, "after": after}
        return None

    async def create_container(self, user_id: str, text: str, reply_to_id: str | None = None) -> str:
        form: dict[str, Any] = {"media_type": "TEXT", "text": text}
        if reply_to_id:
            form["reply_to_id"] = reply_to_id
        data = await self._request("POST", f"/{user_id}/threads", data=form)
        return str(data["id"])

    async def wait_until_finished(self, creation_id: str, timeout_sec: int = 60) -> None:
        deadline = time.monotonic() + timeout_sec
        while True:
            data = await self._request(
                "GET", f"/{creation_id}", params={"fields": "status,error_message"}
            )
            status = data.get("status")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise ThreadsApiError(
                    None, None, None, f"container {creation_id} {status}: {data.get('error_message')}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"container {creation_id} not finished in {timeout_sec}s")
            await self._sleep(POLL_INTERVAL_SEC)

    async def publish(self, user_id: str, creation_id: str) -> str:
        data = await self._request(
            "POST", f"/{user_id}/threads_publish", data={"creation_id": creation_id}
        )
        return str(data["id"])

    async def hide_reply(self, reply_id: str) -> None:
        await self._request("POST", f"/{reply_id}/manage_reply", data={"hide": "true"})

    async def get_publishing_limit(self, user_id: str) -> dict[str, Any]:
        """Return the first item of the publishing-limit listing (quota_usage, config, ...)."""
        data = await self._request(
            "GET", f"/{user_id}/threads_publishing_limit", params={"fields": "quota_usage,config"}
        )
        items = data.get("data") or []
        return dict(items[0]) if items else {}

    async def refresh_token(self) -> dict[str, Any]:
        """Exchange the current long-lived token for a new one (root domain, not /v1.0)."""
        return await self._request(
            "GET",
            "/refresh_access_token",
            params={"grant_type": "th_refresh_token"},
            root=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
