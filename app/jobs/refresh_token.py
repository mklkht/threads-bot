"""Scheduled job: refresh the long-lived Threads access token before it expires."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.alerts import AlertLevel, notify
from app.config import Settings
from app.store import Store
from app.threads_api import ThreadsClient, TokenInvalidError

logger = logging.getLogger("job.refresh_token")

TOKEN_LIFETIME_DAYS = 60
WARN_AFTER_DAYS = 50


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
