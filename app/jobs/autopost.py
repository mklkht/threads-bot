"""Scheduled job: generate and publish one persona post."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from app import filters
from app.alerts import AlertLevel, notify
from app.config import Settings
from app.jobs import is_paused
from app.llm import LLMClient
from app.store import Store
from app.threads_api import ThreadsClient

logger = logging.getLogger("job.autopost")

DEFAULT_QUOTA_TOTAL = 250
QUOTA_SAFETY_MARGIN = 10
POST_SOURCE = "persona"


def pick_topic(topics: list[str], used_recent_first: list[str]) -> str | None:
    """Prefer a topic never used; otherwise the one used longest ago.

    `used_recent_first` is ordered by last use, newest first (see Store.get_used_topics).
    """
    if not topics:
        return None
    unused = [t for t in topics if t not in used_recent_first]
    if unused:
        return random.choice(unused)
    for topic in reversed(used_recent_first):
        if topic in topics:
            return topic
    return topics[0]


async def job_autopost(store: Store, threads: ThreadsClient, llm: LLMClient, settings: Settings) -> None:
    if is_paused(settings):
        logger.info("autopost skipped: PAUSE file present")
        return

    user_id = await store.get_config("user_id")
    if not user_id:
        await notify(settings, AlertLevel.CRITICAL, "autopost: user_id is unknown, skipping")
        return

    limit = await threads.get_publishing_limit(user_id)
    usage = int(limit.get("quota_usage", 0) or 0)
    total = int((limit.get("config") or {}).get("quota_total", DEFAULT_QUOTA_TOTAL) or DEFAULT_QUOTA_TOTAL)
    if usage >= total - QUOTA_SAFETY_MARGIN:
        await notify(settings, AlertLevel.WARNING, f"autopost skipped: publishing quota {usage}/{total}")
        return

    topics = settings.niche_topics_list()
    topic = pick_topic(topics, await store.get_used_topics(50))
    if topic is None:
        await notify(settings, AlertLevel.WARNING, "autopost skipped: NICHE_TOPICS is empty")
        return

    recent = [p["text"] for p in await store.get_recent_posts(10)]
    text = await llm.generate_post(topic, recent)
    ok, reason = filters.check_post_text(text, settings.post_max_chars)
    if not ok:
        logger.info("autopost: first draft rejected (%s), retrying once", reason)
        text = await llm.generate_post(topic, recent, feedback=f"перевірка не пройдена: {reason}")
        ok, reason = filters.check_post_text(text, settings.post_max_chars)
    if not ok:
        post_id = await store.create_post(text, topic, POST_SOURCE, "failed")
        await store.update_post(post_id, error=f"filter:{reason}")
        await notify(settings, AlertLevel.WARNING, f"autopost failed filters ({reason}), topic={topic}")
        return

    post_id = await store.create_post(text, topic, POST_SOURCE, "generated")

    if settings.dry_run:
        await store.update_post(post_id, status="dry_run")
        logger.info("[DRY_RUN] would publish post topic=%s text=%r", topic, text)
        return

    try:
        creation_id = await threads.create_container(user_id, text)
        await store.update_post(post_id, creation_id=creation_id, status="container_created")
        await threads.wait_until_finished(creation_id)
        threads_id = await threads.publish(user_id, creation_id)
        await store.update_post(
            post_id,
            threads_id=threads_id,
            status="published",
            published_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("autopost published post_id=%s threads_id=%s topic=%s", post_id, threads_id, topic)
    except Exception as exc:
        await store.update_post(post_id, status="failed", error=str(exc)[:500])
        await notify(settings, AlertLevel.CRITICAL, f"autopost publish failed (post_id={post_id}): {exc}")
