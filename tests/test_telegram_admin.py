"""Telegram approval: drafts stay off Threads until the publish button."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.jobs.autopost import create_draft, publish_draft
from app.prompts import build_post_prompt
from app.store import Store
from app.telegram_admin import (
    TelegramAdmin,
    merge_idea,
    parse_callback,
    parse_post_command,
)


class StubLLM:
    def __init__(self, text: str = "Чернетка про конкретний PR.") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def generate_post(
        self,
        topic: str,
        recent_posts: list[str],
        feedback: str | None = None,
        idea: str | None = None,
    ) -> str:
        self.calls.append({"topic": topic, "idea": idea, "feedback": feedback, "recent": list(recent_posts)})
        return self.text


class StubThreads:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def get_publishing_limit(self, user_id: str) -> dict[str, Any]:
        return {"quota_usage": 0, "config": {"quota_total": 250}}

    async def create_container(self, user_id: str, text: str, reply_to_id: str | None = None) -> str:
        return "c1"

    async def wait_until_finished(self, creation_id: str, timeout_sec: int = 60) -> None:
        return None

    async def publish(self, user_id: str, creation_id: str) -> str:
        self.published.append((user_id, creation_id))
        return "tid-1"


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 10

    async def __call__(self, method: str, payload: dict[str, Any]) -> Any:
        self.calls.append((method, payload))
        if method == "sendMessage":
            self._next_id += 1
            return {"message_id": self._next_id}
        if method == "getUpdates":
            return []
        return True

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def _admin(settings: Settings, store: Store, llm: StubLLM, threads: StubThreads, api: FakeApi, dry_run: bool = True) -> TelegramAdmin:
    tuned = settings.model_copy(
        update={"alert_telegram_bot_token": "tok", "alert_telegram_chat_id": "99", "dry_run": dry_run}
    )
    return TelegramAdmin(store, threads, llm, tuned, api=api)  # type: ignore[arg-type]


def _message(text: str, chat: int = 99, reply_to: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"text": text, "chat": {"id": chat}}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": message}


def _callback(data: str, chat: int = 99, message_id: int = 11) -> dict[str, Any]:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "q1",
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}},
        },
    }


def test_parse_post_command_and_callback() -> None:
    assert parse_post_command("/post") == parse_post_command("/post@smith_th_bot")
    assert parse_post_command("/post@smith_th_bot").idea is None  # type: ignore[union-attr]
    parsed = parse_post_command("/post стендап, де всі майже готові")
    assert parsed is not None and parsed.idea == "стендап, де всі майже готові"
    assert parse_post_command("/help") is None
    assert parse_callback("p:12") == ("p", 12)
    assert parse_callback("nope") is None


def test_merge_idea_keeps_the_original() -> None:
    assert merge_idea(None, "  зроби сухіше  ") == "зроби сухіше"
    assert "Правка: зроби сухіше" in merge_idea("стендап", "зроби сухіше")


def test_prompt_keeps_the_author_idea() -> None:
    prompt = build_post_prompt("C# та .NET", ["старий пост"], idea="рахунок Azure на 3 долари")
    assert "рахунок Azure на 3 долари" in prompt
    assert "старий пост" in prompt
    assert "Не підміняй іншою темою" in prompt


@pytest.mark.asyncio
async def test_foreign_chat_is_ignored(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    admin = _admin(settings, tmp_store, StubLLM(), StubThreads(), api)
    await admin.handle_update(_message("/post секрет", chat=1))
    assert api.calls == []


@pytest.mark.asyncio
async def test_post_command_stores_draft_and_does_not_publish(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    threads = StubThreads()
    llm = StubLLM()
    admin = _admin(settings, tmp_store, llm, threads, api)
    await admin.handle_update(_message("/post стендап"))
    drafts = await tmp_store.posts_by_status("draft")
    assert len(drafts) == 1
    assert drafts[0]["idea"] == "стендап"
    assert drafts[0]["text"] == "Чернетка про конкретний PR."
    assert llm.calls[0]["idea"] == "стендап"
    assert threads.published == []
    send = next(payload for method, payload in api.calls if method == "sendMessage")
    assert "Чернетка про конкретний PR." in send["text"]
    assert send["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"p:{drafts[0]['id']}"


@pytest.mark.asyncio
async def test_new_post_supersedes_the_open_draft(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    admin = _admin(settings, tmp_store, StubLLM(), StubThreads(), api)
    await admin.handle_update(_message("/post перша"))
    first = (await tmp_store.posts_by_status("draft"))[0]
    await admin.handle_update(_message("/post друга"))
    closed = await tmp_store.get_post(first["id"])
    assert closed is not None and closed["status"] == "superseded"
    assert len(await tmp_store.posts_by_status("draft")) == 1
    assert "editMessageText" in api.methods()


@pytest.mark.asyncio
async def test_reject_and_reply_and_regen(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    llm = StubLLM()
    admin = _admin(settings, tmp_store, llm, StubThreads(), api)
    await admin.handle_update(_message("/post стендап"))
    draft = (await tmp_store.posts_by_status("draft"))[0]
    message_id = int(draft["tg_message_id"])

    await admin.handle_update(_callback(f"n:{draft['id']}", message_id=message_id))
    rejected = await tmp_store.get_post(draft["id"])
    assert rejected is not None and rejected["status"] == "rejected"

    await admin.handle_update(_message("зроби сухіше", reply_to=message_id))
    revised = (await tmp_store.posts_by_status("draft"))[0]
    assert "стендап" in (revised["idea"] or "")
    assert "зроби сухіше" in (revised["idea"] or "")

    await admin.handle_update(_callback(f"r:{revised['id']}", message_id=int(revised["tg_message_id"])))
    again = (await tmp_store.posts_by_status("draft"))[0]
    assert again["id"] != revised["id"]
    assert again["idea"] == revised["idea"]


@pytest.mark.asyncio
async def test_publish_button_calls_threads_once(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    threads = StubThreads()
    admin = _admin(settings, tmp_store, StubLLM(), threads, api, dry_run=False)
    await tmp_store.set_config("user_id", "42")
    await admin.handle_update(_message("/post"))
    draft = (await tmp_store.posts_by_status("draft"))[0]
    await admin.handle_update(_callback(f"p:{draft['id']}", message_id=int(draft["tg_message_id"])))
    published = await tmp_store.get_post(draft["id"])
    assert published is not None and published["status"] == "published"
    assert threads.published == [("42", "c1")]

    await admin.handle_update(_callback(f"p:{draft['id']}", message_id=int(draft["tg_message_id"])))
    assert threads.published == [("42", "c1")]


@pytest.mark.asyncio
async def test_dry_run_publish_stays_off_threads(settings: Settings, tmp_store: Store) -> None:
    threads = StubThreads()
    admin = _admin(settings, tmp_store, StubLLM(), threads, FakeApi(), dry_run=True)
    await tmp_store.set_config("user_id", "42")
    await admin.handle_update(_message("/post"))
    draft = (await tmp_store.posts_by_status("draft"))[0]
    result = await publish_draft(tmp_store, threads, admin._settings, draft["id"])  # noqa: SLF001
    assert result.ok and result.detail == "dry_run"
    assert threads.published == []


@pytest.mark.asyncio
async def test_pause_blocks_a_draft(settings: Settings, tmp_store: Store) -> None:
    Path(settings.db_path).parent.joinpath("PAUSE").write_text("")
    result = await create_draft(tmp_store, StubLLM(), settings)  # type: ignore[arg-type]
    assert result.post_id is None
    assert result.error is not None and "паузі" in result.error
