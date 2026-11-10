"""Tests for the SQLite store layer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.store import Store


@pytest.mark.asyncio
async def test_create_and_update_post(tmp_store: Store) -> None:
    post_id = await tmp_store.create_post("текст", "C# та .NET", "persona", "generated")
    await tmp_store.update_post(post_id, status="published", threads_id="t1")
    async with tmp_store._conn.execute("SELECT status, threads_id FROM posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "published"
    assert row["threads_id"] == "t1"


@pytest.mark.asyncio
async def test_update_post_rejects_unknown_column(tmp_store: Store) -> None:
    post_id = await tmp_store.create_post("текст", None, "persona", "generated")
    with pytest.raises(ValueError):
        await tmp_store.update_post(post_id, bogus="x")


@pytest.mark.asyncio
async def test_recent_posts_only_published_and_used_topics(tmp_store: Store) -> None:
    p1 = await tmp_store.create_post("перший", "A", "persona", "generated")
    await tmp_store.update_post(p1, status="published")
    await tmp_store.create_post("чорновик", "B", "persona", "failed")
    p3 = await tmp_store.create_post("третій", "A", "persona", "generated")
    await tmp_store.update_post(p3, status="published")

    recent = await tmp_store.get_recent_posts(10)
    assert {p["text"] for p in recent} == {"перший", "третій"}
    topics = await tmp_store.get_used_topics(10)
    assert set(topics) == {"A", "B"}


@pytest.mark.asyncio
async def test_config_and_token_roundtrip(tmp_store: Store) -> None:
    assert await tmp_store.get_config("user_id") is None
    await tmp_store.set_config("user_id", "42")
    await tmp_store.set_config("user_id", "43")
    assert await tmp_store.get_config("user_id") == "43"

    assert await tmp_store.get_token_info() is None
    await tmp_store.save_token("tok", "2026-01-01T00:00:00+00:00", None)
    info = await tmp_store.get_token_info()
    assert info is not None
    assert info["access_token"] == "tok"
    assert info["refreshed_at"] == "2026-01-01T00:00:00+00:00"
    assert info["expires_at"] is None


@pytest.mark.asyncio
async def test_reply_exists_and_count_per_user(tmp_store: Store) -> None:
    assert not await tmp_store.reply_exists("r1")
    await tmp_store.create_reply_pending("r1", "post1", "post1", "alice", "hi", "reply", "ok", "answer")
    assert await tmp_store.reply_exists("r1")
    assert await tmp_store.count_our_replies_to_user("post1", "alice") == 0  # still pending

    await tmp_store.mark_reply_done("r1", our_reply_id="our1")
    assert await tmp_store.count_our_replies_to_user("post1", "alice") == 1
    assert await tmp_store.count_our_replies_to_user("post1", "bob") == 0

    await tmp_store.create_reply_pending("r2", "post1", "post1", "alice", "spam", "hide_spam", "ad", None)
    await tmp_store.mark_reply_done("r2")
    assert await tmp_store.count_our_replies_to_user("post1", "alice") == 1


@pytest.mark.asyncio
async def test_mark_failed_and_expire_stale_pending(tmp_store: Store) -> None:
    await tmp_store.create_reply_pending("old", "post1", "post1", "u", "t", "reply", None, "a")
    await tmp_store.create_reply_pending("fresh", "post1", "post1", "u", "t", "reply", None, "a")
    await tmp_store.create_reply_pending("failing", "post1", "post1", "u", "t", "reply", None, "a")

    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    await tmp_store._conn.execute("UPDATE replies SET created_at = ? WHERE reply_id = ?", (old_ts, "old"))
    await tmp_store._conn.commit()

    expired = await tmp_store.expire_stale_pending(10)
    assert expired == 1

    async with tmp_store._conn.execute("SELECT reply_id, status FROM replies ORDER BY reply_id") as cur:
        statuses = {row["reply_id"]: row["status"] for row in await cur.fetchall()}
    assert statuses == {"failing": "pending", "fresh": "pending", "old": "failed"}

    await tmp_store.mark_reply_failed("failing", "boom")
    async with tmp_store._conn.execute("SELECT status, reason FROM replies WHERE reply_id = ?", ("failing",)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"
    assert row["reason"] == "boom"
