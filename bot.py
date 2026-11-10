"""Entrypoint: configure logging, verify the Threads account, run the scheduler until shutdown."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.alerts import AlertLevel, notify
from app.config import Settings
from app.jobs import data_dir
from app.jobs.autopost import job_autopost
from app.jobs.check_replies import job_check_replies
from app.jobs.refresh_token import job_refresh_token, verify_account
from app.llm import LLMClient
from app.store import Store
from app.threads_api import ThreadsClient

HEARTBEAT_INTERVAL_MIN = 5
TZ_NAME = "Europe/Kyiv"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def _guarded(name: str, fn: Callable[..., Awaitable[None]], *args: Any) -> Callable[[], Awaitable[None]]:
    """Wrap a job so an unexpected exception is logged and never kills the scheduler."""

    async def runner() -> None:
        try:
            await fn(*args)
        except Exception:
            logging.getLogger("scheduler").exception("job %s failed", name)

    return runner


async def _touch_heartbeat(path: Path) -> None:
    path.touch()


async def main() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    log = logging.getLogger("bot")

    store = Store(settings.db_path)
    await store.init()
    threads = ThreadsClient(store, settings.threads_api_base)
    llm = LLMClient(settings)

    async def shutdown_resources() -> None:
        await threads.aclose()
        await store.close()

    try:
        me = await verify_account(store, threads, settings.threads_access_token)
    except Exception as exc:
        log.critical("startup check failed: cannot read account (%s)", type(exc).__name__)
        await shutdown_resources()
        return 1
    await store.set_config("user_id", str(me["id"]))
    await store.set_config("username", str(me.get("username", "")))
    log.info("connected as @%s (id=%s), dry_run=%s", me.get("username"), me["id"], settings.dry_run)
    await notify(settings, AlertLevel.INFO, f"bot started as @{me.get('username')}, dry_run={settings.dry_run}")

    await job_refresh_token(store, threads, settings)

    scheduler = AsyncIOScheduler(timezone=TZ_NAME)
    scheduler.add_job(
        _guarded("autopost", job_autopost, store, threads, llm, settings),
        CronTrigger.from_crontab(settings.post_cron, timezone=TZ_NAME),
        jitter=settings.post_jitter_sec,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
        id="autopost",
    )
    scheduler.add_job(
        _guarded("check_replies", job_check_replies, store, threads, llm, settings),
        IntervalTrigger(minutes=settings.replies_interval_min),
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        id="check_replies",
    )
    scheduler.add_job(
        _guarded("refresh_token", job_refresh_token, store, threads, settings),
        CronTrigger(hour=4, minute=17, timezone=TZ_NAME),
        misfire_grace_time=86400,
        coalesce=True,
        max_instances=1,
        id="refresh_token",
    )

    heartbeat = data_dir(settings) / "heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    scheduler.add_job(
        partial(_touch_heartbeat, heartbeat),
        IntervalTrigger(minutes=HEARTBEAT_INTERVAL_MIN),
        max_instances=1,
        id="heartbeat",
    )
    await _touch_heartbeat(heartbeat)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    scheduler.start()
    log.info("scheduler started, post_cron=%r", settings.post_cron)
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        scheduler.shutdown(wait=True)
        await shutdown_resources()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
