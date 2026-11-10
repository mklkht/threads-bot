"""Scheduled job: refresh the long-lived Threads access token before it expires."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.alerts import AlertLevel, notify
from app.config import Settings
from app.store import Store
from app.threads_api import ThreadsClient, TokenInvalidError

logger = logging.getLogger("job.refresh_token")

TOKEN_LIFETIME_DAYS = 60
WARN_AFTER_DAYS = 50
TOKEN_SOURCE_KEY = "token_source_sha"
PLACEHOLDER_TOKENS = frozenset({"", "REPLACE_ME"})


class AccountReader(Protocol):
    async def get_me(self) -> dict[str, Any]: ...


def _is_placeholder(token: str | None) -> bool:
    return token is None or token.strip() in PLACEHOLDER_TOKENS


def _source_sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def bootstrap_token(store: Store, configured: str, *, force: bool = False) -> None:
    """Store the operator-supplied token when it is new or the database has nothing usable.

    A token refreshed into the database stays there until `.env` changes.
    Pass force after the API has rejected the stored token and `configured` is a different real value.
    """
    if _is_placeholder(configured):
        return
    info = await store.get_token_info()
    stored = None if info is None else str(info["access_token"])
    source = await store.get_config(TOKEN_SOURCE_KEY)
    if force or _is_placeholder(stored) or source != _source_sha(configured):
        now = datetime.now(timezone.utc).isoformat()
        await store.save_token(configured, now, None)
        await store.set_config(TOKEN_SOURCE_KEY, _source_sha(configured))


async def verify_account(store: Store, threads: AccountReader, configured: str) -> dict[str, Any]:
    """Confirm the Threads account, adopting `configured` when the stored token cannot.

    Placeholder values such as REPLACE_ME are replaced before the first call.
    If a previously stored token is rejected and settings hold a different token,
    that token is saved and the call is retried once.
    """
    await bootstrap_token(store, configured)
    try:
        return await threads.get_me()
    except TokenInvalidError:
        info = await store.get_token_info()
        stored = "" if info is None else str(info["access_token"])
        if stored == configured or _is_placeholder(configured):
            raise
        logger.warning("stored access token was rejected; trying the token from settings")
        await bootstrap_token(store, configured, force=True)
        return await threads.get_me()


async def job_refresh_token(store: Store, threads: ThreadsClient, settings: Settings) -> None:
    now = datetime.now(timezone.utc)
    info = await store.get_token_info()
    first_run = info is None
    if info is None:
        await store.save_token(settings.threads_access_token, now.isoformat(), None)
        info = await store.get_token_info()
    assert info is not None  # set just above

    refreshed_at = datetime.fromisoformat(str(info["refreshed_at"]))
    age_days = (now - refreshed_at).total_seconds() / 86400
    if not first_run and age_days < settings.token_refresh_days:
        return

    try:
        result = await threads.refresh_token()
        expires_at = now + timedelta(seconds=int(result.get("expires_in", 0)))
        await store.save_token(str(result["access_token"]), now.isoformat(), expires_at.isoformat())
        logger.info("access token refreshed, expires_at=%s", expires_at.isoformat())
    except TokenInvalidError:
        await notify(settings, AlertLevel.CRITICAL, "токен недійсний, потрібен ручний рефреш")
    except Exception as exc:
        left = max(TOKEN_LIFETIME_DAYS - age_days, 0)
        if age_days >= WARN_AFTER_DAYS:
            await notify(
                settings,
                AlertLevel.WARNING,
                f"не вдалось оновити токен ({type(exc).__name__}), лишилось ~{left:.0f} днів",
            )
        else:
            logger.warning("token refresh failed (%s), will retry later", type(exc).__name__)
