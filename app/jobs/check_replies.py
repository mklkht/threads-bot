"""Scheduled job: triage comments under our recent posts and reply / hide / ignore."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from app import filters
from app.alerts import AlertLevel, notify
from app.config import Settings
from app.jobs import is_paused
from app.llm import LLMClient, ReplyDecision
from app.store import Store
from app.threads_api import ThreadsClient, TokenInvalidError

logger = logging.getLogger("job.check_replies")

REPLY_QUOTA_LIMIT = 900
STALE_PENDING_MINUTES = 10
COLD_START_KEY = "cold_start_done"
URL_MARKER = "http"


async def _cold_start(store: Store, threads: ThreadsClient, posts: list[dict[str, Any]]) -> None:
    """First run: record every existing reply as skipped so old comments are never answered."""
    for post in posts:
        for reply in await threads.get_conversation(post["id"]):
            if reply.get("is_reply_owned_by_me") or await store.reply_exists(reply["id"]):
                continue
            await store.create_reply_pending(
                reply["id"],
                post["id"],
                (reply.get("replied_to") or {}).get("id"),
                reply.get("username"),
                reply.get("text"),
                "skip",
                "cold_start",
                None,
            )
            await store.mark_reply_done(reply["id"])
    await store.set_config(COLD_START_KEY, "true")
    logger.info("cold start complete, existing replies marked as skipped")


def _decide_quota_limited(reply_text: str) -> ReplyDecision:
    """Low reply quota: no LLM. Hide obvious link spam, ignore everything else."""
    if URL_MARKER in reply_text.lower():
        return ReplyDecision(action="hide_spam", reason="reply_quota_low_link_heuristic")
    return ReplyDecision(action="ignore", reason="reply_quota_low")


async def _decide(
    store: Store,
    llm: LLMClient,
    settings: Settings,
    post: dict[str, Any],
    reply: dict[str, Any],
    quota_limited: bool,
) -> ReplyDecision:
    username = reply.get("username") or ""
    reply_text = reply.get("text") or ""
    if quota_limited:
        return _decide_quota_limited(reply_text)
    if await store.count_our_replies_to_user(post["id"], username) >= settings.replies_max_per_user:
        return ReplyDecision(action="ignore", reason="user_limit_reached")
    return await llm.classify_reply(post["text"], reply_text, username, thread_tail=[])


async def _send_reply(
    store: Store,
    threads: ThreadsClient,
    settings: Settings,
    user_id: str,
    reply: dict[str, Any],
    reply_text: str,
) -> None:
    delay = random.randint(settings.reply_delay_min_sec, settings.reply_delay_max_sec)
    await asyncio.sleep(delay)
    if settings.dry_run:
        logger.info("[DRY_RUN] would reply to %s: %r", reply["id"], reply_text)
        await store.mark_reply_done(reply["id"])
        return
    creation_id = await threads.create_container(user_id, reply_text, reply_to_id=reply["id"])
    await threads.wait_until_finished(creation_id)
    our_reply_id = await threads.publish(user_id, creation_id)
    await store.mark_reply_done(reply["id"], our_reply_id=our_reply_id)


async def _apply_action(
    store: Store,
    threads: ThreadsClient,
    settings: Settings,
    user_id: str,
    reply: dict[str, Any],
    decision: ReplyDecision,
) -> None:
    if decision.action == "reply":
        await _send_reply(store, threads, settings, user_id, reply, decision.reply_text or "")
    elif decision.action == "hide_spam":
        if settings.dry_run:
            logger.info("[DRY_RUN] would hide reply %s", reply["id"])
        else:
            try:
                await threads.hide_reply(reply["id"])
            except TokenInvalidError:
                raise
            except Exception as exc:  # e.g. nested reply that cannot be hidden
                logger.info("hide failed for %s (%s), downgrading to ignore", reply["id"], type(exc).__name__)
                await store.update_reply_action(reply["id"], "ignore", f"hide_failed:{type(exc).__name__}")
        await store.mark_reply_done(reply["id"])
    else:
        await store.mark_reply_done(reply["id"])


async def job_check_replies(store: Store, threads: ThreadsClient, llm: LLMClient, settings: Settings) -> None:
    if is_paused(settings):
        logger.info("check_replies skipped: PAUSE file present")
        return

    user_id = await store.get_config("user_id")
    if not user_id:
        await notify(settings, AlertLevel.CRITICAL, "check_replies: user_id is unknown, skipping")
        return

    posts = await threads.get_my_threads(settings.replies_posts_limit)

    if await store.get_config(COLD_START_KEY) is None:
        await _cold_start(store, threads, posts)
        return

    limit = await threads.get_publishing_limit(user_id)
    quota_limited = int(limit.get("reply_quota_usage", 0) or 0) > REPLY_QUOTA_LIMIT
    if quota_limited:
        await notify(settings, AlertLevel.WARNING, "reply quota high: LLM disabled for replies this cycle")

    processed = 0
    budget_left = True
    for post in posts:
        if not budget_left:
            break
        conv = await threads.get_conversation(post["id"])
        our_reply_ids = {r["id"] for r in conv if r.get("is_reply_owned_by_me")}
        for reply in conv:
            if reply.get("is_reply_owned_by_me") or await store.reply_exists(reply["id"]):
                continue
            replied_to_id = (reply.get("replied_to") or {}).get("id")
            if replied_to_id != post["id"] and replied_to_id not in our_reply_ids:
                await store.create_reply_pending(
                    reply["id"], post["id"], replied_to_id, reply.get("username"),
                    reply.get("text"), "skip", "not_our_thread", None,
                )
                await store.mark_reply_done(reply["id"])
                continue
            if processed >= settings.replies_max_per_cycle:
                budget_left = False
                break

            processed += 1
            try:
                decision = await _decide(store, llm, settings, post, reply, quota_limited)
                if decision.action == "reply":
                    ok, why = filters.check_reply_text(decision.reply_text or "", settings.reply_max_chars)
                    if not ok:
                        decision = ReplyDecision(action="ignore", reason=f"filter:{why}")
                await store.create_reply_pending(
                    reply["id"],
                    post["id"],
                    replied_to_id,
                    reply.get("username"),
                    reply.get("text"),
                    decision.action,
                    decision.reason,
                    decision.reply_text,
                )
                await _apply_action(store, threads, settings, user_id, reply, decision)
            except TokenInvalidError:
                raise
            except Exception as exc:
                logger.exception("failed to process reply %s", reply.get("id"))
                await store.mark_reply_failed(reply["id"], str(exc))

    await store.expire_stale_pending(STALE_PENDING_MINUTES)
