"""Tests for LLM provider setup."""

from __future__ import annotations

import logging
import sys
from types import ModuleType

from app.config import Settings
from app.llm import GEMINI_THINKING_OFF, GeminiProvider
from bot import RedactSecretsFilter


def test_gemini_client_uses_v1beta(monkeypatch: object, settings: Settings) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_genai = ModuleType("google.genai")
    fake_genai.Client = FakeClient  # type: ignore[attr-defined]
    fake_google = ModuleType("google")
    fake_google.genai = fake_genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    GeminiProvider(settings)

    assert captured["http_options"] == {"api_version": "v1beta"}
    assert captured["api_key"] == "test-llm-key"


def test_log_redacts_access_token() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: %s %s",
        args=("GET", "https://graph.threads.net/v1.0/me?access_token=THAASECRET&fields=id"),
        exc_info=None,
    )
    assert RedactSecretsFilter().filter(record)
    message = record.getMessage()
    assert "THAASECRET" not in message
    assert "access_token=[redacted]" in message


async def test_gemini_disables_thinking(monkeypatch: object, settings: Settings) -> None:
    calls: list[dict[str, object]] = []

    class Models:
        async def generate_content(self, **kwargs: object) -> object:
            calls.append(kwargs)

            class Response:
                text = "готовий пост"

            return Response()

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.aio = self
            self.models = Models()

    fake_genai = ModuleType("google.genai")
    fake_genai.Client = FakeClient  # type: ignore[attr-defined]
    fake_google = ModuleType("google")
    fake_google.genai = fake_genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    provider = GeminiProvider(settings)
    text = await provider.generate_post("system", "user")

    assert text == "готовий пост"
    config = calls[0]["config"]
    assert isinstance(config, dict)
    assert config["thinking_config"] == GEMINI_THINKING_OFF
