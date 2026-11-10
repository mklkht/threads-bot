"""Tests for post and reply text filters."""

from __future__ import annotations

import pytest

from app.filters import check_post_text, check_reply_text


@pytest.mark.parametrize(
    "text",
    [
        "Сьогодні знову дебажив async deadlock. Нарешті зрозумів, де помилка.",
        "Angular 19 вийшов, перевіряю standalone компоненти 👀",
    ],
)
def test_valid_post_passes(text: str) -> None:
    ok, reason = check_post_text(text, max_chars=400)
    assert ok and reason is None


def test_post_too_long_fails() -> None:
    ok, reason = check_post_text("а" * 401, max_chars=400)
    assert not ok and reason is not None and reason.startswith("too_long")


def test_post_at_limit_passes() -> None:
    ok, _ = check_post_text("а" * 400, max_chars=400)
    assert ok


def test_empty_text_fails() -> None:
    ok, reason = check_post_text("   ", max_chars=400)
    assert not ok and reason == "empty"


@pytest.mark.parametrize("text", ["пост #dotnet", "#"])
def test_hashtag_fails(text: str) -> None:
    ok, reason = check_post_text(text, max_chars=400)
    assert not ok and reason == "hashtag"


@pytest.mark.parametrize("text", ["дивись https://example.com", "http://x.y"])
def test_url_fails(text: str) -> None:
    ok, reason = check_post_text(text, max_chars=400)
    assert not ok and reason == "url"


def test_two_emoji_fail_one_passes() -> None:
    ok, reason = check_post_text("круто 🔥🚀", max_chars=400)
    assert not ok and reason == "too_many_emoji"
    ok, _ = check_post_text("круто 🔥", max_chars=400)
    assert ok


@pytest.mark.parametrize("word", ["Безумовно", "у сучасному світі", "ПІДСУМОВУЮЧИ", "варто зазначити", "таким чином"])
def test_stopwords_fail_case_insensitive(word: str) -> None:
    ok, reason = check_post_text(f"Текст, де є {word} всередині.", max_chars=400)
    assert not ok and reason is not None and reason.startswith("stopword:")


def test_reply_passes_when_clean() -> None:
    ok, reason = check_reply_text("дякую, а який у тебе досвід із цим?", max_chars=250)
    assert ok and reason is None


def test_reply_mention_fails() -> None:
    ok, reason = check_reply_text("@someone дивись сюди", max_chars=250)
    assert not ok and reason == "mention"


def test_post_mention_is_allowed() -> None:
    ok, _ = check_post_text("а @someone тут нормально", max_chars=400)
    assert ok


def test_reply_too_long_fails() -> None:
    ok, reason = check_reply_text("б" * 251, max_chars=250)
    assert not ok and reason is not None and reason.startswith("too_long")
