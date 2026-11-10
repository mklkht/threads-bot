"""LLM access: post generation and reply classification behind a small provider interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.prompts import (
    build_classify_prompt,
    build_post_prompt,
    build_public_reply_prompt,
    build_reply_system_prompt,
    build_system_prompt,
)

logger = logging.getLogger("llm")

DECIDE_TOOL_NAME = "decide"
POST_MAX_TOKENS = 400
CLASSIFY_MAX_TOKENS = 400
# gemini-2.5 counts hidden thinking against max_output_tokens and cuts the post mid-sentence.
GEMINI_THINKING_OFF = {"thinking_budget": 0}


class ReplyDecision(BaseModel):
    action: Literal["reply", "ignore", "hide_spam"]
    reason: str = Field(max_length=200)
    reply_text: str | None = Field(default=None, max_length=250)


class LLMProvider(ABC):
    """Interface every LLM backend implements."""

    @abstractmethod
    async def generate_post(self, system: str, user: str) -> str: ...

    @abstractmethod
    async def classify_reply(self, system: str, user: str) -> ReplyDecision: ...


class AnthropicProvider(LLMProvider):
    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.llm_api_key)
        self._model_post = settings.llm_model_post
        self._model_classify = settings.llm_model_classify

    async def generate_post(self, system: str, user: str) -> str:
        resp = await self._client.messages.create(
            model=self._model_post,
            max_tokens=POST_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        texts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
        return "".join(texts).strip()

    async def classify_reply(self, system: str, user: str) -> ReplyDecision:
        tool: dict[str, Any] = {
            "name": DECIDE_TOOL_NAME,
            "description": "Записати рішення щодо коментаря: reply, ignore або hide_spam.",
            "input_schema": ReplyDecision.model_json_schema(),
        }
        resp = await self._client.messages.create(
            model=self._model_classify,
            max_tokens=CLASSIFY_MAX_TOKENS,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": DECIDE_TOOL_NAME},
            messages=[{"role": "user", "content": user}],
        )
        tool_input: Any = None
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == DECIDE_TOOL_NAME:
                tool_input = block.input
                break
        if tool_input is None:
            logger.warning("classify: no tool_use block in response")
            return ReplyDecision(action="ignore", reason="invalid_llm_output")
        try:
            return ReplyDecision.model_validate(tool_input)
        except ValidationError:
            logger.warning("classify: tool input failed validation")
            return ReplyDecision(action="ignore", reason="invalid_llm_output")


class GeminiProvider(LLMProvider):
    def __init__(self, settings: Settings) -> None:
        from google import genai  # lazy import: only needed when LLM_PROVIDER=gemini

        # system_instruction is rejected on v1 ("Developer instruction is not enabled").
        self._client = genai.Client(
            api_key=settings.llm_api_key,
            http_options={"api_version": "v1beta"},
        )
        self._model_post = settings.llm_model_post
        self._model_classify = settings.llm_model_classify

    async def generate_post(self, system: str, user: str) -> str:
        resp = await self._client.aio.models.generate_content(
            model=self._model_post,
            contents=user,
            config={
                "system_instruction": system,
                "max_output_tokens": POST_MAX_TOKENS,
                "temperature": 1.15,
                "thinking_config": GEMINI_THINKING_OFF,
            },
        )
        return (resp.text or "").strip()

    async def classify_reply(self, system: str, user: str) -> ReplyDecision:
        resp = await self._client.aio.models.generate_content(
            model=self._model_classify,
            contents=user,
            config={
                "system_instruction": system,
                "max_output_tokens": CLASSIFY_MAX_TOKENS,
                "thinking_config": GEMINI_THINKING_OFF,
                "response_mime_type": "application/json",
                "response_schema": ReplyDecision,
            },
        )
        try:
            return ReplyDecision.model_validate_json(resp.text or "")
        except (ValidationError, ValueError):
            logger.warning("classify: gemini response failed validation")
            return ReplyDecision(action="ignore", reason="invalid_llm_output")


class OpenAIProvider(LLMProvider):
    """Placeholder. Structure is ready; implement when needed (LLM_PROVIDER=openai)."""

    def __init__(self, settings: Settings) -> None:
        raise NotImplementedError("OpenAI provider is not implemented yet; додати при потребі")

    async def generate_post(self, system: str, user: str) -> str:
        raise NotImplementedError

    async def classify_reply(self, system: str, user: str) -> ReplyDecision:
        raise NotImplementedError


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings)
    if settings.llm_provider == "gemini":
        return GeminiProvider(settings)
    if settings.llm_provider == "openai":
        return OpenAIProvider(settings)
    raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")


class LLMClient:
    """Facade used by jobs: builds prompts from persona and delegates to the provider."""

    def __init__(self, settings: Settings) -> None:
        self._persona = settings.account_persona
        self._system = build_system_prompt(settings.account_persona)
        self._provider = build_provider(settings)

    async def generate_public_reply(
        self,
        target_text: str,
        hint: str | None = None,
        feedback: str | None = None,
        avoid: list[str] | None = None,
    ) -> str:
        system = build_reply_system_prompt(self._persona)
        user = build_public_reply_prompt(target_text, hint, feedback, avoid)
        return await self._provider.generate_post(system, user)

    async def generate_post(
        self,
        topic: str,
        recent_posts: list[str],
        feedback: str | None = None,
        idea: str | None = None,
    ) -> str:
        user = build_post_prompt(topic, recent_posts, feedback, idea=idea)
        return await self._provider.generate_post(self._system, user)

    async def classify_reply(
        self,
        post_text: str,
        reply_text: str,
        username: str,
        thread_tail: list[str],
    ) -> ReplyDecision:
        user = build_classify_prompt(post_text, reply_text, username, thread_tail)
        return await self._provider.classify_reply(self._system, user)
