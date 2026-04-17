"""LLM Client — async OpenAI-compatible API with model failover and retry.

Adapted from cortex-agent/src/agent/llm.py.
Supports any OpenAI-compatible endpoint: OpenAI, OpenRouter, FastRouter, vLLM, Anthropic via gateway.

Features:
  - Primary → fallback model failover on 429 / 5xx
  - Exponential backoff retry on rate limits
  - Token usage tracking per call
  - Streaming support
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

import openai

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Structured response from a single LLM call."""

    text: str = ""
    finish_reason: str = "stop"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""


class LLMClient:
    """Async LLM client with failover and retry.

    Usage::

        client = LLMClient(
            model="gpt-4o",
            api_key="sk-...",
            base_url="https://api.openai.com/v1",
            fallback_model="gpt-4o-mini",
        )
        resp = await client.chat(messages=[...])
        print(resp.text)
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        max_tokens: int = 4096,
        temperature: float = 0.2,
        max_retries: int = 3,
        fallback_model: str = "",
        fallback_api_key: str = "",
        fallback_base_url: str = "",
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.fallback_model = fallback_model

        self._primary = openai.AsyncOpenAI(
            api_key=api_key or "not-set",
            base_url=base_url or None,
        )
        self._fallback: openai.AsyncOpenAI | None = None
        if fallback_model:
            self._fallback = openai.AsyncOpenAI(
                api_key=fallback_api_key or api_key or "not-set",
                base_url=fallback_base_url or base_url or None,
            )

    # ── Public ───────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send a chat completion request with automatic failover."""
        kwargs = self._build_kwargs(
            messages,
            model=model or self.model,
            max_tokens=max_tokens or self.max_tokens,
            temperature=temperature if temperature is not None else self.temperature,
            response_format=response_format,
        )
        result = await self._call_with_failover(kwargs)
        if result is None:
            return LLMResponse(text="Error: all LLM endpoints failed after retries.")
        return self._parse_completion(result)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream the LLM response, yielding text chunks as they arrive."""
        kwargs = self._build_kwargs(
            messages,
            model=model or self.model,
            max_tokens=max_tokens or self.max_tokens,
            temperature=temperature if temperature is not None else self.temperature,
        )
        kwargs["stream"] = True

        try:
            async_stream = await self._primary.chat.completions.create(**kwargs)
            async for chunk in async_stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except openai.APIError as exc:
            logger.error("llm.stream_error error=%s", exc)
            yield f"\n\n[Error streaming response: {exc}]"

    # ── Internal ─────────────────────────────────────────────────────────────

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format:
            kwargs["response_format"] = response_format
        return kwargs

    async def _call_with_failover(self, kwargs: dict[str, Any]) -> Any:
        result = await self._call_with_retry(self._primary, kwargs)
        if result is not None:
            return result

        if self._fallback and self.fallback_model:
            logger.warning("llm.failover from=%s to=%s", kwargs.get("model"), self.fallback_model)
            fallback_kwargs = {**kwargs, "model": self.fallback_model}
            return await self._call_with_retry(self._fallback, fallback_kwargs)

        return None

    async def _call_with_retry(
        self, client: openai.AsyncOpenAI, kwargs: dict[str, Any]
    ) -> Any:
        for attempt in range(self.max_retries):
            try:
                return await client.chat.completions.create(**kwargs)
            except openai.RateLimitError:
                if attempt == self.max_retries - 1:
                    logger.error("llm.rate_limited model=%s attempts=%d", kwargs.get("model"), self.max_retries)
                    return None
                wait = 2 ** (attempt + 1)
                logger.warning("llm.rate_limit_retry attempt=%d wait=%ds", attempt + 1, wait)
                await asyncio.sleep(wait)
            except openai.APIError as exc:
                logger.error("llm.api_error model=%s error=%s", kwargs.get("model"), exc)
                return None
        return None

    def _parse_completion(self, completion: Any) -> LLMResponse:
        if not completion.choices:
            return LLMResponse()
        choice = completion.choices[0]
        usage = completion.usage
        return LLMResponse(
            text=choice.message.content or "",
            finish_reason=choice.finish_reason or "stop",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            model=completion.model or "",
        )

    @classmethod
    def from_settings(cls, s: Any) -> "LLMClient":
        """Construct from a Settings.llm config object."""
        return cls(
            model=s.model,
            api_key=s.api_key,
            base_url=s.base_url,
            max_tokens=s.max_tokens,
            temperature=s.temperature,
            max_retries=s.max_retries,
            fallback_model=s.fallback_model,
            fallback_api_key=s.fallback_api_key,
            fallback_base_url=s.fallback_base_url,
        )
