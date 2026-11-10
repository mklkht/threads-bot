"""Pre-publication checks for post and reply text."""

from __future__ import annotations

import re

from app.prompts import STOPWORDS

# Emoji code point ranges (pictographs, symbols, dingbats, supplemental symbols).
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001f2ff"  # mahjong, domino, playing cards, enclosed ideographs
    "\U0001f300-\U0001faff"  # pictographs, emoticons, transport, supplemental symbols
    "☀-➿"  # misc symbols and dingbats
    "⭐⭕⬆⬇⬅➡"
    "]"
)
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
# C# and F# are language names. A hashtag is a # that starts a word.
_HASHTAG_RE = re.compile(r"(?<![\w])#\w", re.UNICODE)
MAX_EMOJI = 1


def _common_checks(text: str, max_chars: int) -> tuple[bool, str | None]:
    if not text or not text.strip():
        return False, "empty"
    if len(text) > max_chars:
        return False, f"too_long:{len(text)}>{max_chars}"
    if len(_EMOJI_RE.findall(text)) > MAX_EMOJI:
        return False, "too_many_emoji"
    if _HASHTAG_RE.search(text):
        return False, "hashtag"
    if _URL_RE.search(text):
        return False, "url"
    lowered = text.lower()
    for word in STOPWORDS:
        if word in lowered:
            return False, f"stopword:{word}"
    return True, None


def check_post_text(text: str, max_chars: int) -> tuple[bool, str | None]:
    """Return (ok, reason). reason is None when ok."""
    return _common_checks(text, max_chars)


def explain_rejection(reason: str) -> str:
    """Ukrainian hint passed back to the model when a draft is rejected."""
    if reason == "too_many_emoji":
        return "занадто багато емодзі, залиш не більше одного або жодного"
    if reason == "hashtag":
        return "є хештег. Назви на кшталт C# можна, слова що починаються з # не можна"
    if reason == "url":
        return "є посилання, прибери його"
    if reason == "empty":
        return "текст порожній, напиши сам пост"
    if reason.startswith("too_long"):
        return "текст довший за ліміт, скороти"
    if reason.startswith("stopword:"):
        word = reason.split(":", 1)[1]
        return f"є заборонена фраза «{word}», перепиши без неї"
    return reason


def check_reply_text(text: str, max_chars: int) -> tuple[bool, str | None]:
    """Return (ok, reason). Replies additionally must not mention other accounts."""
    ok, reason = _common_checks(text, max_chars)
    if not ok:
        return ok, reason
    if "@" in text:
        return False, "mention"
    return True, None
