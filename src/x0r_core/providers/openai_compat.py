from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import get_settings
from .base import Completion, Provider, ToolCall, Usage


class OpenAICompatibleProvider(Provider):
    """Works with any OpenAI-compatible API: OpenAI, OpenRouter, Groq, Together,
    vLLM, Ollama, LM Studio, Azure OpenAI (with a deployment base URL)."""

    name = "openai_compatible"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.timeout = settings.request_timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model or get_settings().chat_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            data = resp.json()

        choice = (data.get("choices") or [{}])[0].get("message") or {}
        raw_calls = choice.get("tool_calls") or []
        calls: list[ToolCall] = []
        for call in raw_calls:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(id=call.get("id", ""), name=fn.get("name", ""), arguments=args))

        usage = data.get("usage") or {}
        return Completion(
            content=choice.get("content"),
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]] | None:
        if not texts:
            return []
        payload = {"model": model or get_settings().embedding_model, "input": texts}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings", json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data.get("data", [])]
