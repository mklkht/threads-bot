"""Generate a draft and publish it only after an explicit approval."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from app import filters
from app.config import Settings
from app.jobs import is_paused
from app.llm import LLMClient
from app.store import Store
from app.threads_api import ThreadsClient

logger = logging.getLogger("job.autopost")

DEFAULT_QUOTA_TOTAL = 250
QUOTA_SAFETY_MARGIN = 10
POST_SOURCE = "persona"
REPLY_SOURCE = "public_reply"


@dataclass
class DraftResult:
    post_id: int | None = None
    text: str | None = None
    error: str | None = None


@dataclass
class PublishResult:
    ok: bool
    detail: str


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


async def create_draft(
    store: Store,
    llm: LLMClient,
    settings: Settings,
    *,
    idea: str | None = None,
    topic: str | None = None,
) -> DraftResult:
    """Generate one draft and store it. Does not call the Threads publish API."""
    if is_paused(settings):
        logger.info("draft skipped: PAUSE file present")
        return DraftResult(error="бот на паузі, файл data/PAUSE")

    chosen = topic
    if not idea and not chosen:
        chosen = pick_topic(settings.niche_topics_list(), await store.get_used_topics(50))
        if chosen is None:
            return DraftResult(error="список тем порожній")

    recent = await store.get_avoid_texts(10)
    text = await llm.generate_post(chosen or "", recent, idea=idea)
    ok, reason = filters.check_post_text(text, settings.post_max_chars)
    if not ok:
        logger.info("draft: first version rejected (%s), retrying once", reason)
        text = await llm.generate_post(
            chosen or "", recent, feedback=filters.explain_rejection(reason), idea=idea
        )
        ok, reason = filters.check_post_text(text, settings.post_max_chars)
    if not ok:
        await store.create_post(text, chosen, POST_SOURCE, "failed")
        logger.info("draft failed filters (%s)", reason)
        return DraftResult(error=f"чернетка не пройшла перевірку: {filters.explain_rejection(reason or '')}")

    post_id = await store.create_post(text, chosen, POST_SOURCE, "draft")
    if idea:
        await store.update_post(post_id, idea=idea)
    logger.info("draft created post_id=%s", post_id)
    return DraftResult(post_id=post_id, text=text)


async def create_reply_draft(
    store: Store,
    llm: LLMClient,
    settings: Settings,
    *,
    target_text: str,
    reply_to_id: str,
    permalink: str | None,
    hint: str | None = None,
    avoid: list[str] | None = None,
) -> DraftResult:
    """Draft a public reply. Does not publish it."""
    if is_paused(settings):
        return DraftResult(error="бот на паузі, файл data/PAUSE")

    text = await llm.generate_public_reply(target_text, hint, avoid=avoid)
    ok, reason = filters.check_reply_text(text, settings.reply_max_chars)
    if not ok:
        logger.info("reply draft rejected (%s), retrying once", reason)
        text = await llm.generate_public_reply(
            target_text, hint, feedback=filters.explain_rejection(reason or ""), avoid=avoid
        )
        ok, reason = filters.check_reply_text(text, settings.reply_max_chars)
    if not ok:
        await store.create_post(text, None, REPLY_SOURCE, "failed")
        return DraftResult(error=f"відповідь не пройшла перевірку: {filters.explain_rejection(reason or '')}")

    post_id = await store.create_post(text, None, REPLY_SOURCE, "draft")
    await store.update_post(
        post_id,
        idea=hint,
        reply_to_threads_id=reply_to_id,
        target_text=target_text,
        target_permalink=permalink,
    )
    logger.info("reply draft created post_id=%s", post_id)
    return DraftResult(post_id=post_id, text=text)


async def publish_draft(store: Store, threads: ThreadsClient, settings: Settings, post_id: int) -> PublishResult:
    """Publish a draft. A second call after success does not publish again."""
    post = await store.get_post(post_id)
    if post is None:
        return PublishResult(False, "чернетка не знайдена")
    if post["status"] != "draft":
        return PublishResult(False, "ця чернетка вже закрита")
    if is_paused(settings):
        return PublishResult(False, "бот на паузі")

    user_id = await store.get_config("user_id")
    if not user_id:
        return PublishResult(False, "акаунт ще не підключений")

    limit = await threads.get_publishing_limit(user_id)
    usage = int(limit.get("quota_usage", 0) or 0)
    total = int((limit.get("config") or {}).get("quota_total", DEFAULT_QUOTA_TOTAL) or DEFAULT_QUOTA_TOTAL)
    if usage >= total - QUOTA_SAFETY_MARGIN:
        return PublishResult(False, f"ліміт публікацій {usage}/{total}")

    if settings.dry_run:
        await store.update_post(post_id, status="dry_run")
        logger.info("[DRY_RUN] would publish post_id=%s", post_id)
        return PublishResult(True, "dry_run")

    await store.update_post(post_id, status="publishing")
    try:
        creation_id = await threads.create_container(
            user_id, post["text"], reply_to_id=post.get("reply_to_threads_id")
        )
        await store.update_post(post_id, creation_id=creation_id)
        await threads.wait_until_finished(creation_id)
        threads_id = await threads.publish(user_id, creation_id)
    except Exception as exc:
        logger.exception("publish failed post_id=%s (%s)", post_id, type(exc).__name__)
        await store.update_post(post_id, status="draft", error=type(exc).__name__)
        detail = "не вдалося опублікувати, спробуй ще раз"
        if "permission" in str(exc).lower() or "threads_keyword_search" in str(exc).lower():
            detail = "Threads не дав відповісти на чужий пост. У додатку Meta потрібен дозвіл threads_keyword_search і новий токен."
        return PublishResult(False, detail)

    await store.update_post(
        post_id,
        threads_id=threads_id,
        status="published",
        published_at=datetime.now(timezone.utc).isoformat(),
    )
    logger.info("published post_id=%s threads_id=%s", post_id, threads_id)
    return PublishResult(True, "published")
