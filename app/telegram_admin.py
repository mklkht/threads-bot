"""Telegram commands that approve a draft before it goes to Threads.

Long polling only: the NAS does not accept inbound webhooks. Only the chat id
from settings may talk to the bot.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import unquote, urljoin

import httpx

from app.config import Settings
from app.jobs.autopost import create_draft, create_reply_draft, publish_draft
from app.llm import LLMClient
from app.store import Store
from app.threads_api import ThreadsApiError, ThreadsClient

logger = logging.getLogger("telegram")

OFFSET_KEY = "telegram_update_offset"
MAX_IDEA = 800
PACK_SIZE = 3
Api = Callable[[str, dict[str, Any]], Awaitable[Any]]


class TelegramApiError(Exception):
    def __init__(self, method: str, status: int) -> None:
        super().__init__(method)
        self.method = method
        self.status = status


_THREAD_POST_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/@(?P<user>[A-Za-z0-9._]+)/post/"
    r"(?P<code>[A-Za-z0-9_-]+)/?(?:[?#][^\s]*)?"
)
_THREAD_SHARE_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/share/(?P<code>[A-Za-z0-9_-]+)/?(?:[?#][^\s]*)?"
)
_THREADS_PAGE_RE = re.compile(r"https?://(?:www\.)?threads\.(?:net|com)(?:[/?#]|$)", re.IGNORECASE)
_CANONICAL_TAG_RE = re.compile(
    r"<link\b[^>]*\brel=[\"']canonical[\"'][^>]*>|<meta\b[^>]*\bproperty=[\"']og:url[\"'][^>]*>",
    re.IGNORECASE,
)
_TAG_URL_RE = re.compile(r"""\b(?:href|content)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
# A browser User-Agent gets a JS shell with no post URL. Link-preview crawlers get a redirect.
_SHARE_USER_AGENT = "TelegramBot (like TwitterBot)"
_SHARE_REDIRECTS = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class PostCommand:
    idea: str | None


@dataclass(frozen=True)
class ReplyCommand:
    username: str
    shortcode: str
    hint: str | None
    share_url: str | None = None


def _parse_idea_command(text: str, command: str) -> PostCommand | None:
    stripped = text.strip()
    if not stripped.startswith(command):
        return None
    first, _, rest = stripped.partition(" ")
    if first.split("@", 1)[0] != command:
        return None
    idea = rest.strip()
    return PostCommand(idea=idea or None)


def parse_post_command(text: str) -> PostCommand | None:
    """Return the idea when text is /post, including /post@BotName. None otherwise."""
    return _parse_idea_command(text, "/post")


def parse_pack_command(text: str) -> PostCommand | None:
    """Return the shared idea when text is /pack. None otherwise."""
    return _parse_idea_command(text, "/pack")


def _hint_around(text: str, match: re.Match[str]) -> str | None:
    hint = " ".join(f"{text[: match.start()]} {text[match.end() :]}".split())
    return hint or None


def _share_url(match: re.Match[str]) -> str:
    url = match.group(0).split("#", 1)[0].split("?", 1)[0]
    return url[:-1] if url.endswith("/") else url


def parse_reply_command(text: str) -> ReplyCommand | None:
    """Return the Threads link target when text is /reply. None otherwise.

    Accepts a post URL (@user/post/code) or a /share/ link on threads.com or threads.net.
    """
    stripped = text.strip()
    if not stripped.startswith("/reply"):
        return None
    first, _, rest = stripped.partition(" ")
    if first.split("@", 1)[0] != "/reply":
        return None
    post_match = _THREAD_POST_RE.search(rest)
    share_match = _THREAD_SHARE_RE.search(rest)
    if post_match is not None and (share_match is None or post_match.start() <= share_match.start()):
        return ReplyCommand(
            username=post_match.group("user"),
            shortcode=post_match.group("code"),
            hint=_hint_around(rest, post_match),
        )
    if share_match is not None:
        return ReplyCommand(
            username="",
            shortcode="",
            hint=_hint_around(rest, share_match),
            share_url=_share_url(share_match),
        )
    return ReplyCommand(username="", shortcode="", hint=rest.strip() or None)


def post_target_from_url(url: str) -> tuple[str, str] | None:
    """Return @username and the post shortcode when url is a Threads post link."""
    match = _THREAD_POST_RE.search(unescape(unquote(url)))
    if match is None:
        return None
    return match.group("user"), match.group("code")


def post_target_from_html(body: str) -> tuple[str, str] | None:
    """Read @username/post/shortcode from a canonical or og:url tag, then the page."""
    text = unescape(body)
    for tag in _CANONICAL_TAG_RE.findall(text):
        for value in _TAG_URL_RE.findall(tag):
            found = post_target_from_url(value)
            if found is not None:
                return found
    return post_target_from_url(text)


def _threads_page(url: str) -> bool:
    return _THREADS_PAGE_RE.match(url) is not None


async def resolve_share_link(url: str, client: httpx.AsyncClient | None = None) -> tuple[str, str] | None:
    """Follow a Threads /share/ link until it becomes @username and a post shortcode.

    The share code is not the post shortcode. Threads redirects link-preview
    crawlers to the post; a browser User-Agent gets a shell page without it.
    """
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    current = url
    headers = {"User-Agent": _SHARE_USER_AGENT, "Accept": "text/html"}
    try:
        for _ in range(5):
            if not _threads_page(current):
                return None
            try:
                response = await http.get(current, headers=headers, follow_redirects=False)
            except httpx.HTTPError as exc:
                logger.warning("share link resolve failed (%s)", type(exc).__name__)
                return None
            if response.status_code in _SHARE_REDIRECTS:
                location = response.headers.get("location")
                if not location:
                    return post_target_from_html(response.text)
                current = urljoin(str(response.url), location.strip())
                found = post_target_from_url(current)
                if found is not None:
                    return found
                continue
            found = post_target_from_url(str(response.url))
            if found is not None:
                return found
            return post_target_from_html(response.text)
        return None
    finally:
        if owns_client:
            await http.aclose()


def parse_callback(data: str) -> tuple[str, int] | None:
    action, _, raw_id = data.partition(":")
    if action not in {"p", "r", "n"} or not raw_id.isdigit():
        return None
    return action, int(raw_id)


def merge_idea(previous: str | None, note: str) -> str:
    """A reply to a draft is a revision of the idea, not the post text itself."""
    cleaned = " ".join(note.split())
    if not previous or not previous.strip():
        return cleaned[:MAX_IDEA]
    return f"{previous.strip()}\nПравка: {cleaned}"[:MAX_IDEA]


def draft_keyboard(post_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "Опублікувати", "callback_data": f"p:{post_id}"},
            {"text": "Інший варіант", "callback_data": f"r:{post_id}"},
            {"text": "Ні", "callback_data": f"n:{post_id}"},
        ]]
    }


def format_reply_message(text: str, target_text: str, label: str | None = None) -> str:
    short = " ".join(target_text.split())
    if len(short) > 140:
        short = short[:137] + "..."
    quoted = short or "(пост без тексту)"
    head = label or "Відповідь"
    return f"{head}\nна: {quoted}\n\n{text}"


def format_draft_message(text: str, idea: str | None, label: str | None = None) -> str:
    head = label or "Чернетка"
    if idea:
        short = " ".join(idea.split())
        if len(short) > 180:
            short = short[:177] + "..."
        head = f"{head}\nідея: {short}"
    return f"{head}\n\n{text}"


HELP = (
    "Команди:\n"
    "/post — одна чернетка\n"
    "/post ідея — чернетка з твоєї ідеї\n"
    "/pack — три варіанти, щоб вибрати\n"
    "/pack ідея — три варіанти однієї ідеї\n"
    "/reply посилання — три варіанти відповіді під цим постом\n"
    "Відповідь на чернетку — новий варіант з цією правкою\n"
    "Двічі на день бот сам надсилає чернетку. У Threads нічого не йде без кнопки Опублікувати.\n"
    "Кнопки: Опублікувати, Інший варіант, Ні"
)

REPLY_USAGE = "Надішли /reply і посилання на пост. Можна дописати, якою має бути відповідь."
SHARE_UNRESOLVED = (
    "Не вдалося відкрити це посилання. Надішли пряме посилання на пост: threads.com/@ім'я/post/код."
)


class TelegramAdmin:
    def __init__(
        self,
        store: Store,
        threads: ThreadsClient,
        llm: LLMClient,
        settings: Settings,
        api: Api | None = None,
    ) -> None:
        self._store = store
        self._threads = threads
        self._llm = llm
        self._settings = settings
        self._api_override = api
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._settings.alert_telegram_bot_token and self._settings.alert_telegram_chat_id)

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            logger.warning("telegram admin off: alert token or chat id is empty")
            return
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))
        ready = False
        try:
            while not stop.is_set():
                try:
                    if not ready:
                        await self._skip_backlog()
                        await self._set_commands()
                        ready = True
                    updates = await self._poll()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("telegram poll failed (%s)", type(exc).__name__)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=5)
                    except TimeoutError:
                        continue
                    return
                for update in updates:
                    try:
                        await self.handle_update(update)
                    except Exception:
                        logger.exception("telegram update %s failed", update.get("update_id"))
                    await self._store.set_config(OFFSET_KEY, str(int(update["update_id"]) + 1))
        finally:
            await self._client.aclose()

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if chat is None:
            return
        if not self._allowed(chat):
            return
        text = message.get("text") or ""
        command = parse_post_command(text)
        if command is not None:
            await self._propose(idea=command.idea)
            return
        pack = parse_pack_command(text)
        if pack is not None:
            await self._offer_pack(pack.idea)
            return
        reply_command = parse_reply_command(text)
        if reply_command is not None:
            await self._start_reply(reply_command)
            return
        if text.startswith("/start") or text.startswith("/help"):
            await self._send(str(chat), HELP)
            return
        reply = message.get("reply_to_message") or {}
        if text.strip() and reply.get("message_id") is not None:
            post = await self._store.get_post_by_tg_message(str(chat), str(reply["message_id"]))
            if post is not None:
                await self._replace_one(post, hint=text)
                return
        if text.strip():
            await self._send(str(chat), "Напиши /post, /pack, або /reply і посилання на пост.")

    async def offer_scheduled_draft(self) -> None:
        """Send one post draft. Skip when the chat already has something to approve."""
        if not self.enabled:
            return
        if await self._store.posts_by_status("draft"):
            logger.info("scheduled draft skipped: a draft is still open")
            return
        await self._propose(idea=None, replace_all=False)

    async def _offer_pack(self, idea: str | None) -> None:
        await self._close_drafts("superseded", "замінено новою пачкою")
        topics = list(self._settings.niche_topics_list())
        random.shuffle(topics)
        for index in range(PACK_SIZE):
            topic = None if idea or not topics else topics[index % len(topics)]
            await self._propose(
                idea=idea,
                topic=topic,
                replace_all=False,
                label=f"Варіант {index + 1} з {PACK_SIZE}",
            )

    async def _propose(
        self,
        *,
        idea: str | None,
        topic: str | None = None,
        replace_all: bool = True,
        label: str | None = None,
    ) -> None:
        chat = str(self._settings.alert_telegram_chat_id)
        if replace_all:
            await self._close_drafts("superseded", "замінено новою чернеткою")
        result = await create_draft(self._store, self._llm, self._settings, idea=idea, topic=topic)
        if result.error or result.post_id is None or result.text is None:
            await self._send(chat, result.error or "не вдалося зібрати чернетку")
            return
        sent = await self._api(
            "sendMessage",
            {
                "chat_id": chat,
                "text": format_draft_message(result.text, idea, label),
                "reply_markup": draft_keyboard(result.post_id),
            },
        )
        message_id = str(sent["message_id"])
        await self._store.update_post(result.post_id, tg_chat_id=chat, tg_message_id=message_id)
        logger.info("draft offered post_id=%s", result.post_id)

    async def _start_reply(self, command: ReplyCommand) -> None:
        chat = str(self._settings.alert_telegram_chat_id)
        username = command.username
        shortcode = command.shortcode
        if not shortcode and command.share_url:
            resolved = await resolve_share_link(command.share_url)
            if resolved is None:
                logger.warning("share link did not resolve to a post")
                await self._send(chat, SHARE_UNRESOLVED)
                return
            username, shortcode = resolved
            logger.info("share link resolved @%s", username)
        if not shortcode:
            await self._send(chat, REPLY_USAGE)
            return
        try:
            found = await self._threads.find_public_post(username, shortcode)
        except ThreadsApiError:
            logger.warning("public post lookup failed @%s", username)
            await self._send(
                chat,
                "Не бачу цей пост. Потрібен публічний профіль від 100 підписників і дозвіл threads_profile_discovery в Meta.",
            )
            return
        if not found or not found.get("id"):
            await self._send(chat, "Не знайшов цей пост серед останніх публікацій профілю.")
            return
        await self._close_drafts("superseded", "замінено новою пачкою")
        avoid: list[str] = []
        for index in range(PACK_SIZE):
            text = await self._offer_reply(
                reply_to_id=str(found["id"]),
                target_text=str(found.get("text") or ""),
                permalink=str(found.get("permalink") or ""),
                hint=command.hint,
                avoid=list(avoid),
                label=f"Варіант {index + 1} з {PACK_SIZE}",
            )
            if text:
                avoid.append(text)

    async def _offer_reply(
        self,
        *,
        reply_to_id: str,
        target_text: str,
        permalink: str | None,
        hint: str | None,
        avoid: list[str] | None = None,
        replace_all: bool = False,
        label: str | None = None,
    ) -> str | None:
        chat = str(self._settings.alert_telegram_chat_id)
        if replace_all:
            await self._close_drafts("superseded", "замінено новою чернеткою")
        result = await create_reply_draft(
            self._store,
            self._llm,
            self._settings,
            target_text=target_text,
            reply_to_id=reply_to_id,
            permalink=permalink,
            hint=hint,
            avoid=avoid,
        )
        if result.error or result.post_id is None or result.text is None:
            await self._send(chat, result.error or "не вдалося зібрати відповідь")
            return None
        sent = await self._api(
            "sendMessage",
            {
                "chat_id": chat,
                "text": format_reply_message(result.text, target_text, label),
                "reply_markup": draft_keyboard(result.post_id),
            },
        )
        await self._store.update_post(
            result.post_id,
            tg_chat_id=chat,
            tg_message_id=str(sent["message_id"]),
        )
        logger.info("reply draft offered post_id=%s", result.post_id)
        return result.text

    async def _on_callback(self, query: dict[str, Any]) -> None:
        query_id = query["id"]
        message = query.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if not self._allowed(chat):
            await self._answer(query_id, None)
            return
        parsed = parse_callback(str(query.get("data") or ""))
        if parsed is None:
            await self._answer(query_id, "невідома кнопка")
            return
        action, post_id = parsed
        post = await self._store.get_post(post_id)
        if post is None or post["status"] != "draft":
            await self._answer(query_id, "ця чернетка вже закрита")
            return
        if action == "n":
            await self._store.update_post(post_id, status="rejected")
            await self._edit(str(chat), str(message.get("message_id") or post.get("tg_message_id")), f"{post['text']}\n\nвідхилено")
            await self._answer(query_id, "відхилено")
            return
        if action == "r":
            await self._answer(query_id, "роблю інший варіант")
            await self._replace_one(post)
            return
        await self._answer(query_id, "публікую")
        result = await publish_draft(self._store, self._threads, self._settings, post_id)
        if not result.ok:
            await self._send(str(chat), result.detail)
            return
        footer = "опубліковано в Threads" if result.detail == "published" else "dry_run: у Threads не публікував"
        await self._edit(str(chat), str(message.get("message_id") or post["tg_message_id"]), f"{post['text']}\n\n{footer}")
        if post.get("reply_to_threads_id"):
            await self._close_same_target(str(post["reply_to_threads_id"]))

    async def _replace_one(self, post: dict[str, Any], hint: str | None = None) -> None:
        """Replace a single open card. Sibling drafts in a pack stay open."""
        if post["status"] == "draft":
            await self._retire(post, "замінено іншим варіантом")
        if post.get("reply_to_threads_id"):
            merged = merge_idea(post.get("idea"), hint) if hint else post.get("idea")
            await self._offer_reply(
                reply_to_id=str(post["reply_to_threads_id"]),
                target_text=str(post.get("target_text") or ""),
                permalink=post.get("target_permalink"),
                hint=merged,
                avoid=[str(post.get("text") or "")],
            )
            return
        idea = merge_idea(post.get("idea"), hint) if hint else post.get("idea")
        topic = None if idea else post.get("topic")
        await self._propose(idea=idea, topic=topic, replace_all=False)

    async def _retire(self, post: dict[str, Any], footer: str) -> None:
        await self._store.update_post(post["id"], status="superseded")
        if post.get("tg_chat_id") and post.get("tg_message_id"):
            await self._edit(str(post["tg_chat_id"]), str(post["tg_message_id"]), f"{post['text']}\n\n{footer}")

    async def _close_same_target(self, reply_to_id: str) -> None:
        for other in await self._store.posts_by_status("draft"):
            if other.get("reply_to_threads_id") != reply_to_id:
                continue
            await self._store.update_post(other["id"], status="superseded")
            if other.get("tg_chat_id") and other.get("tg_message_id"):
                await self._edit(
                    str(other["tg_chat_id"]),
                    str(other["tg_message_id"]),
                    f"{other['text']}\n\nзакрито, бо інший варіант уже опубліковано",
                )

    async def _close_drafts(self, status: str, footer: str) -> None:
        for post in await self._store.posts_by_status("draft"):
            await self._store.update_post(post["id"], status=status)
            if post.get("tg_chat_id") and post.get("tg_message_id"):
                await self._edit(post["tg_chat_id"], post["tg_message_id"], f"{post['text']}\n\n{footer}")

    def _allowed(self, chat_id: object) -> bool:
        expected = self._settings.alert_telegram_chat_id
        return expected is not None and chat_id is not None and str(chat_id) == str(expected)

    async def _send(self, chat_id: str, text: str) -> None:
        await self._api("sendMessage", {"chat_id": chat_id, "text": text})

    async def _answer(self, query_id: str, text: str | None) -> None:
        payload: dict[str, Any] = {"callback_query_id": query_id}
        if text:
            payload["text"] = text
        try:
            await self._api("answerCallbackQuery", payload)
        except TelegramApiError:
            logger.warning("telegram answerCallbackQuery failed")

    async def _edit(self, chat_id: str, message_id: str, text: str) -> None:
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return
        try:
            await self._api(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": mid,
                    "text": text,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except TelegramApiError:
            logger.warning("telegram edit failed message_id=%s", message_id)

    async def _set_commands(self) -> None:
        try:
            await self._api(
                "setMyCommands",
                {"commands": [
                    {"command": "post", "description": "Запропонувати пост"},
                    {"command": "pack", "description": "Три варіанти поста"},
                    {"command": "reply", "description": "Три відповіді під постом"},
                ]},
            )
        except TelegramApiError:
            logger.warning("telegram setMyCommands failed")

    async def _skip_backlog(self) -> None:
        """Drop updates queued before this admin existed, so an old /post does not fire."""
        if await self._store.get_config(OFFSET_KEY) is not None:
            return
        offset: int | None = None
        last: int | None = None
        while True:
            payload: dict[str, Any] = {"timeout": 0, "limit": 100}
            if offset is not None:
                payload["offset"] = offset
            batch = await self._api("getUpdates", payload)
            if not isinstance(batch, list) or not batch:
                break
            last = int(batch[-1]["update_id"])
            offset = last + 1
            if len(batch) < 100:
                break
        await self._store.set_config(OFFSET_KEY, str((last + 1) if last is not None else 0))

    async def _poll(self) -> list[dict[str, Any]]:
        saved = await self._store.get_config(OFFSET_KEY)
        payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if saved not in (None, "0"):
            payload["offset"] = int(saved)
        result = await self._api("getUpdates", payload)
        return result if isinstance(result, list) else []

    async def _api(self, method: str, payload: dict[str, Any]) -> Any:
        if self._api_override is not None:
            return await self._api_override(method, payload)
        if self._client is None:
            raise TelegramApiError(method, 0)
        token = self._settings.alert_telegram_bot_token
        response = await self._client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or not body.get("ok"):
            logger.warning("telegram %s failed: status=%s", method, response.status_code)
            raise TelegramApiError(method, response.status_code)
        return body.get("result")
