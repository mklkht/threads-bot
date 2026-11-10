"""Tests for LLM provider setup."""

from __future__ import annotations

import sys
from types import ModuleType

from app.config import Settings
from app.llm import GeminiProvider


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
