"""Async SQLite storage layer built on aiosqlite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  creation_id TEXT, threads_id TEXT UNIQUE, text TEXT NOT NULL,
  topic TEXT, source TEXT NOT NULL, status TEXT NOT NULL,
  error TEXT, created_at TEXT NOT NULL, published_at TEXT
);
CREATE TABLE IF NOT EXISTS replies (
  reply_id TEXT PRIMARY KEY, post_id TEXT NOT NULL, replied_to_id TEXT,
  username TEXT, text TEXT, action TEXT NOT NULL, reason TEXT,
  reply_text TEXT, our_reply_id TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL, processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_replies_post_user ON replies(post_id, username);
"""

POST_COLUMNS = frozenset(
    {"creation_id", "threads_id", "text", "topic", "source", "status", "error", "created_at", "published_at"}
)

TOKEN_KEY = "threads_access_token"
TOKEN_REFRESHED_KEY = "token_refreshed_at"
TOKEN_EXPIRES_KEY = "token_expires_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.init() must be called before use")
        return self._db

    # ---- config ----

    async def get_config(self, key: str) -> str | None:
        async with self._conn.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_config(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now()),
        )
        await self._conn.commit()

    async def get_token_info(self) -> dict[str, Any] | None:
        token = await self.get_config(TOKEN_KEY)
        if token is None:
            return None
        return {
            "access_token": token,
            "refreshed_at": await self.get_config(TOKEN_REFRESHED_KEY),
            "expires_at": await self.get_config(TOKEN_EXPIRES_KEY),
        }

    async def save_token(self, access_token: str, refreshed_at: str, expires_at: str | None) -> None:
        await self.set_config(TOKEN_KEY, access_token)
        await self.set_config(TOKEN_REFRESHED_KEY, refreshed_at)
        if expires_at is not None:
            await self.set_config(TOKEN_EXPIRES_KEY, expires_at)

    # ---- posts ----

    async def create_post(self, text: str, topic: str | None, source: str, status: str) -> int:
        cur = await self._conn.execute(
            "INSERT INTO posts (text, topic, source, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (text, topic, source, status, _now()),
        )
        await self._conn.commit()
        return int(cur.lastrowid)

    async def update_post(self, post_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - POST_COLUMNS
        if unknown:
            raise ValueError(f"Unknown post columns: {sorted(unknown)}")
        assignments = ", ".join(f"{col} = ?" for col in fields)
        values = [*fields.values(), post_id]
        await self._conn.execute(f"UPDATE posts SET {assignments} WHERE id = ?", values)
        await self._conn.commit()

    async def get_recent_posts(self, limit: int = 10) -> list[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT * FROM posts WHERE status = 'published' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_used_topics(self, limit: int = 10) -> list[str]:
        """Distinct topics ordered by most recent use first (newest topic at index 0)."""
        async with self._conn.execute(
            "SELECT topic FROM posts WHERE topic IS NOT NULL "
            "GROUP BY topic ORDER BY MAX(created_at) DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [r["topic"] for r in rows]

    # ---- replies ----

    async def reply_exists(self, reply_id: str) -> bool:
        async with self._conn.execute("SELECT 1 FROM replies WHERE reply_id = ?", (reply_id,)) as cur:
            return (await cur.fetchone()) is not None

    async def create_reply_pending(
        self,
        reply_id: str,
        post_id: str,
        replied_to_id: str | None,
        username: str | None,
        text: str | None,
        action: str,
        reason: str | None,
        reply_text: str | None,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO replies (reply_id, post_id, replied_to_id, username, text, action, reason, "
            "reply_text, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (reply_id, post_id, replied_to_id, username, text, action, reason, reply_text, _now()),
        )
        await self._conn.commit()

    async def update_reply_action(self, reply_id: str, action: str, reason: str | None) -> None:
        await self._conn.execute(
            "UPDATE replies SET action = ?, reason = ? WHERE reply_id = ?",
            (action, reason, reply_id),
        )
        await self._conn.commit()

    async def mark_reply_done(self, reply_id: str, our_reply_id: str | None = None) -> None:
        await self._conn.execute(
            "UPDATE replies SET status = 'done', our_reply_id = ?, processed_at = ? WHERE reply_id = ?",
            (our_reply_id, _now(), reply_id),
        )
        await self._conn.commit()

    async def mark_reply_failed(self, reply_id: str, error_reason: str) -> None:
        """Mark a reply as failed; the error text is stored in the `reason` column."""
        await self._conn.execute(
            "UPDATE replies SET status = 'failed', reason = ?, processed_at = ? WHERE reply_id = ?",
            (error_reason[:500], _now(), reply_id),
        )
        await self._conn.commit()

    async def count_our_replies_to_user(self, post_id: str, username: str | None) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM replies WHERE post_id = ? AND username IS ? "
            "AND action = 'reply' AND status = 'done'",
            (post_id, username),
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"])

    async def expire_stale_pending(self, older_than_minutes: int = 10) -> int:
        """Mark pending replies older than N minutes as failed. Returns the number of rows changed."""
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
        cur = await self._conn.execute(
            "UPDATE replies SET status = 'failed', reason = 'stale_pending', processed_at = ? "
            "WHERE status = 'pending' AND created_at < ?",
            (_now(), cutoff_iso),
        )
        await self._conn.commit()
        return cur.rowcount
