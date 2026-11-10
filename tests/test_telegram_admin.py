"""Telegram approval: drafts stay off Threads until the publish button."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.jobs.autopost import create_draft, publish_draft
from app.prompts import build_brainstorm_prompt, build_post_prompt, build_system_prompt
from app.store import Store
from app.telegram_admin import (
    PROFILE_DISCOVERY_DENIED,
    PUBLIC_LOOKUP_FAILED,
    PUBLIC_POST_NOT_FOUND,
    SHARE_UNRESOLVED,
    TelegramAdmin,
    merge_idea,
    parse_callback,
    parse_pack_command,
    parse_post_command,
    parse_reply_command,
    post_target_from_html,
    post_target_from_url,
    resolve_share_link,
)
from app.threads_api import ThreadsApiError, TokenInvalidError


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
        spark: str | None = None,
    ) -> str:
        self.calls.append(
            {"topic": topic, "idea": idea, "feedback": feedback, "recent": list(recent_posts), "spark": spark}
        )
        return self.text

    async def generate_public_reply(
        self,
        target_text: str,
        hint: str | None = None,
        feedback: str | None = None,
        avoid: list[str] | None = None,
    ) -> str:
        self.calls.append({"target": target_text, "hint": hint, "feedback": feedback, "avoid": avoid})
        return "зона комфорту це диван, не стратегія"


class StubThreads:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.reply_to: list[str | None] = []
        self.lookups: list[tuple[str, str]] = []

    async def get_publishing_limit(self, user_id: str) -> dict[str, Any]:
        return {"quota_usage": 0, "config": {"quota_total": 250}}

    async def create_container(self, user_id: str, text: str, reply_to_id: str | None = None) -> str:
        self.reply_to.append(reply_to_id)
        return "c1"

    async def find_public_post(self, username: str, shortcode: str) -> dict[str, Any]:
        self.lookups.append((username, shortcode))
        return {
            "id": "999",
            "text": "вийди із зони комфорту",
            "permalink": f"https://www.threads.net/@{username}/post/{shortcode}",
            "shortcode": shortcode,
            "username": username,
        }

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


def test_parse_reply_command() -> None:
    parsed = parse_reply_command("/reply https://www.threads.net/@foo/post/AbC-12 зроби сухіше")
    assert parsed is not None
    assert parsed.username == "foo"
    assert parsed.shortcode == "AbC-12"
    assert parsed.hint == "зроби сухіше"
    assert parse_reply_command("/post привіт") is None
    bare = parse_reply_command("/reply")
    assert bare is not None and bare.shortcode == "" and bare.share_url is None
    hinted = parse_reply_command("/reply зроби сухіше https://www.threads.com/@foo/post/AbC-12?xmt=secret")
    assert hinted is not None
    assert hinted.username == "foo" and hinted.shortcode == "AbC-12" and hinted.hint == "зроби сухіше"
    assert hinted.share_url is None


def test_parse_post_command_and_callback() -> None:
    assert parse_post_command("/post") == parse_post_command("/post@smith_th_bot")
    assert parse_post_command("/post@smith_th_bot").idea is None  # type: ignore[union-attr]
    parsed = parse_post_command("/post стендап, де всі майже готові")
    assert parsed is not None and parsed.idea == "стендап, де всі майже готові"
    assert parse_post_command("/help") is None
    assert parse_pack_command("/pack") is not None and parse_pack_command("/pack").idea is None  # type: ignore[union-attr]
    assert parse_pack_command("/pack черга в касі").idea == "черга в касі"  # type: ignore[union-attr]
    assert parse_pack_command("/post") is None
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
    assert "впізнати себе" in prompt


def test_post_voice_is_a_recognition_not_a_sketch() -> None:
    system = build_system_prompt("персона")
    assert "так, це я" in system
    assert "есе" in system
    assert "моралі" in system
    assert "хештеги" in system
    assert "з одного боку" in system
    assert "новини" in system
    plain = build_post_prompt("звичка як характер", [])
    assert "міряє на себе" in plain
    assert "не новини" in plain
    assert "не есе" in plain
    lowered = plain.lower()
    assert "rss" not in lowered
    assert "стрічк" not in lowered
    assert "паливо" not in lowered


def test_rough_register_is_allowed_but_not_required() -> None:
    system = build_system_prompt("персона")
    post = build_post_prompt("статус", [])
    idea = build_post_prompt("статус", [], idea="спокійна думка")
    brainstorm = build_brainstorm_prompt("статус", None, [])

    for prompt in (system, post, brainstorm):
        lowered = prompt.lower()
        assert "сарказм" in lowered or "саркастич" in lowered
        assert "не в кожному пості" in lowered
        assert "на ім'я" in lowered

    assert "йобані колхозники" in system
    assert "срані рагулі" in system
    assert "заїбали, блять" in system
    assert "не обов'язковий" in system
    assert "один із трьох-чотирьох" in post
    assert "есе" in system
    assert "моралі" in system
    assert "хештеги" in system
    assert "новини" in system
    assert "ШІ" in system
    assert "не есе" in post
    assert "не новини" in post
    assert "мат не додавай" in idea
    assert "йобані" not in idea


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
async def test_bare_reply_asks_for_a_link(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    admin = _admin(settings, tmp_store, StubLLM(), StubThreads(), api)
    await admin.handle_update(_message("/reply"))
    assert await tmp_store.posts_by_status("draft") == []
    send = next(payload for method, payload in api.calls if method == "sendMessage")
    assert "посилання" in send["text"]


@pytest.mark.asyncio
async def test_reply_command_publishes_under_the_target_post(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    threads = StubThreads()
    admin = _admin(settings, tmp_store, StubLLM(), threads, api, dry_run=False)
    await tmp_store.set_config("user_id", "42")
    await admin.handle_update(_message("/reply https://www.threads.com/@foo/post/AbC12 гостріше"))
    drafts = await tmp_store.posts_by_status("draft")
    assert len(drafts) == 3
    assert {item["reply_to_threads_id"] for item in drafts} == {"999"}
    draft = drafts[0]
    assert draft["target_text"] == "вийди із зони комфорту"
    await admin.handle_update(_callback(f"p:{draft['id']}", message_id=int(draft["tg_message_id"])))
    assert threads.reply_to == ["999"]
    published = await tmp_store.get_post(draft["id"])
    assert published is not None and published["status"] == "published"
    rest = [await tmp_store.get_post(item["id"]) for item in drafts[1:]]
    assert [item["status"] for item in rest if item is not None] == ["superseded", "superseded"]


@pytest.mark.asyncio
async def test_pack_keeps_three_drafts_and_regen_replaces_one(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    admin = _admin(settings, tmp_store, StubLLM(), StubThreads(), api)
    await admin.handle_update(_message("/pack черга"))
    drafts = await tmp_store.posts_by_status("draft")
    assert len(drafts) == 3
    assert all(item["idea"] == "черга" for item in drafts)
    labels = [payload["text"] for method, payload in api.calls if method == "sendMessage"]
    assert any(text.startswith("Варіант 1 з 3") for text in labels)
    first = drafts[0]
    await admin.handle_update(_callback(f"r:{first['id']}", message_id=int(first["tg_message_id"])))
    still = await tmp_store.posts_by_status("draft")
    assert len(still) == 3
    assert first["id"] not in {item["id"] for item in still}
    closed = await tmp_store.get_post(first["id"])
    assert closed is not None and closed["status"] == "superseded"


@pytest.mark.asyncio
async def test_scheduled_draft_waits_until_the_queue_is_empty(settings: Settings, tmp_store: Store) -> None:
    api = FakeApi()
    admin = _admin(settings, tmp_store, StubLLM(), StubThreads(), api)
    await admin.handle_update(_message("/post"))
    before = len(await tmp_store.posts_by_status("draft"))
    await admin.offer_scheduled_draft()
    assert len(await tmp_store.posts_by_status("draft")) == before
    await admin.handle_update(_callback(
        f"n:{(await tmp_store.posts_by_status('draft'))[0]['id']}",
        message_id=int((await tmp_store.posts_by_status("draft"))[0]["tg_message_id"]),
    ))
    await admin.offer_scheduled_draft()
    assert len(await tmp_store.posts_by_status("draft")) == 1


def test_parse_reply_share_links() -> None:
    shapes = [
        "https://www.threads.com/share/F_HRsQ_fH",
        "https://threads.com/share/F_HRsQ_fH",
        "https://www.threads.net/share/F_HRsQ_fH",
        "https://threads.net/share/F_HRsQ_fH",
        "http://www.threads.com/share/F_HRsQ_fH",
    ]
    for url in shapes:
        parsed = parse_reply_command(f"/reply {url}")
        assert parsed is not None
        assert parsed.share_url == url
        assert parsed.username == "" and parsed.shortcode == ""
        assert parsed.hint is None
    after = parse_reply_command("/reply https://www.threads.com/share/F_HRsQ_fH/ зроби сухіше")
    assert after is not None and after.share_url == "https://www.threads.com/share/F_HRsQ_fH"
    assert after.hint == "зроби сухіше"
    before = parse_reply_command("/reply@smith_th_bot коротше https://threads.net/share/F_HRsQ_fH але тепліше")
    assert before is not None and before.share_url == "https://threads.net/share/F_HRsQ_fH"
    assert before.hint == "коротше але тепліше"
    queried = parse_reply_command("/reply https://www.threads.com/share/F_HRsQ_fH?igshid=abc")
    assert queried is not None and queried.share_url == "https://www.threads.com/share/F_HRsQ_fH"
    assert queried.hint is None


def test_post_target_ignores_the_share_code() -> None:
    canonical = '<link rel="canonical" href="https://www.threads.com/&#064;kseniashaaa/post/DfkiUIXjRgK" />'
    assert post_target_from_html(canonical) == ("kseniashaaa", "DfkiUIXjRgK")
    og = '<meta content="https://threads.net/@foo/post/ZZ9" property="og:url" />'
    assert post_target_from_html(og) == ("foo", "ZZ9")
    tracked = "https://www.threads.com/@kseniashaaa/post/DfkiUIXjRgK?xmt=secret"
    assert post_target_from_url(tracked) == ("kseniashaaa", "DfkiUIXjRgK")
    assert post_target_from_html("<html><title>Threads</title></html>") is None


@pytest.mark.asyncio
async def test_resolve_share_link_follows_redirects_to_the_post() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["user-agent"] == "TelegramBot (like TwitterBot)"
        if request.url.host == "www.threads.net":
            return httpx.Response(301, headers={"location": "https://www.threads.com/share/F_HRsQ_fH"})
        if request.url.path == "/share/F_HRsQ_fH":
            return httpx.Response(
                302,
                headers={"location": "https://www.threads.com/@kseniashaaa/post/DfkiUIXjRgK?xmt=secret"},
            )
        raise AssertionError(f"unexpected fetch {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        resolved = await resolve_share_link("https://www.threads.net/share/F_HRsQ_fH", client=client)
    assert resolved == ("kseniashaaa", "DfkiUIXjRgK")
    assert seen == [
        "https://www.threads.net/share/F_HRsQ_fH",
        "https://www.threads.com/share/F_HRsQ_fH",
    ]


@pytest.mark.asyncio
async def test_resolve_share_link_reads_canonical_html() -> None:
    body = (
        "<html><link rel=\"canonical\" "
        "href=\"https://www.threads.com/&#064;kseniashaaa/post/DfkiUIXjRgK\" /></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        resolved = await resolve_share_link("https://www.threads.com/share/F_HRsQ_fH", client=client)
    assert resolved == ("kseniashaaa", "DfkiUIXjRgK")


@pytest.mark.asyncio
async def test_resolve_share_link_stops_when_threads_does_not_name_the_post() -> None:
    def shell(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><title>Threads</title></html>")

    transport = httpx.MockTransport(shell)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await resolve_share_link("https://www.threads.com/share/F_HRsQ_fH", client=client) is None

    def offsite(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/private"})

    transport = httpx.MockTransport(offsite)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await resolve_share_link("https://threads.com/share/F_HRsQ_fH", client=client) is None

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(broken)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await resolve_share_link("https://threads.com/share/F_HRsQ_fH", client=client) is None


@pytest.mark.asyncio
async def test_reply_share_link_looks_up_the_resolved_post(
    settings: Settings, tmp_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(url: str, client: httpx.AsyncClient | None = None) -> tuple[str, str] | None:
        assert url == "https://www.threads.com/share/F_HRsQ_fH"
        return "kseniashaaa", "DfkiUIXjRgK"

    monkeypatch.setattr("app.telegram_admin.resolve_share_link", fake_resolve)
    api = FakeApi()
    threads = StubThreads()
    llm = StubLLM()
    admin = _admin(settings, tmp_store, llm, threads, api)
    await admin.handle_update(_message("/reply https://www.threads.com/share/F_HRsQ_fH коротше"))
    assert threads.lookups == [("kseniashaaa", "DfkiUIXjRgK")]
    drafts = await tmp_store.posts_by_status("draft")
    assert len(drafts) == 3
    assert {item["reply_to_threads_id"] for item in drafts} == {"999"}
    assert llm.calls[0]["hint"] == "коротше"


@pytest.mark.asyncio
async def test_unresolved_share_link_asks_for_a_direct_post_url(
    settings: Settings, tmp_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(url: str, client: httpx.AsyncClient | None = None) -> tuple[str, str] | None:
        return None

    monkeypatch.setattr("app.telegram_admin.resolve_share_link", fake_resolve)
    api = FakeApi()
    threads = StubThreads()
    admin = _admin(settings, tmp_store, StubLLM(), threads, api)
    await admin.handle_update(_message("/reply https://www.threads.com/share/F_HRsQ_fH"))
    assert await tmp_store.posts_by_status("draft") == []
    assert threads.lookups == []
    send = next(payload for method, payload in api.calls if method == "sendMessage")
    assert send["text"] == SHARE_UNRESOLVED


@pytest.mark.asyncio
async def test_direct_post_link_does_not_resolve_a_share(
    settings: Settings, tmp_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(url: str, client: httpx.AsyncClient | None = None) -> tuple[str, str] | None:
        raise AssertionError(url)

    monkeypatch.setattr("app.telegram_admin.resolve_share_link", fake_resolve)
    threads = StubThreads()
    admin = _admin(settings, tmp_store, StubLLM(), threads, FakeApi())
    await admin.handle_update(_message("/reply https://threads.com/@foo/post/AbC12 гостріше"))
    assert threads.lookups == [("foo", "AbC12")]


class _LookupThreads(StubThreads):
    def __init__(self, result: dict[str, Any] | None | Exception) -> None:
        super().__init__()
        self._result = result

    async def find_public_post(self, username: str, shortcode: str) -> dict[str, Any] | None:
        self.lookups.append((username, shortcode))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ThreadsApiError(403, 10, None, "Application does not have permission for this action"),
            PROFILE_DISCOVERY_DENIED,
        ),
        (ThreadsApiError(None, None, None, "transport error: ConnectError"), PUBLIC_LOOKUP_FAILED),
        (TokenInvalidError(401, 190, None, "Error validating access token"), PUBLIC_LOOKUP_FAILED),
    ],
)
async def test_reply_lookup_errors_name_only_a_missing_discovery_permission(
    settings: Settings, tmp_store: Store, error: Exception, expected: str
) -> None:
    api = FakeApi()
    admin = _admin(settings, tmp_store, StubLLM(), _LookupThreads(error), api)
    await admin.handle_update(_message("/reply https://threads.com/@kseniashaaa/post/DfkiUIXjRgK"))
    assert await tmp_store.posts_by_status("draft") == []
    send = next(payload for method, payload in api.calls if method == "sendMessage")
    assert send["text"] == expected
    assert "100" not in send["text"]


@pytest.mark.asyncio
async def test_reply_not_in_recent_posts_is_not_a_permission_error(
    settings: Settings, tmp_store: Store
) -> None:
    api = FakeApi()
    admin = _admin(settings, tmp_store, StubLLM(), _LookupThreads(None), api)
    await admin.handle_update(_message("/reply https://threads.com/@kseniashaaa/post/DfkiUIXjRgK"))
    assert await tmp_store.posts_by_status("draft") == []
    send = next(payload for method, payload in api.calls if method == "sendMessage")
    assert send["text"] == PUBLIC_POST_NOT_FOUND


@pytest.mark.asyncio
async def test_pause_blocks_a_draft(settings: Settings, tmp_store: Store) -> None:
    Path(settings.db_path).parent.joinpath("PAUSE").write_text("")
    result = await create_draft(tmp_store, StubLLM(), settings)  # type: ignore[arg-type]
    assert result.post_id is None
    assert result.error is not None and "паузі" in result.error
