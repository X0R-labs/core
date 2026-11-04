from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from .base import Completion, Provider, ToolCall, Usage

_WORD = re.compile(r"[a-z0-9]+")
_DIM = 256


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class MockProvider(Provider):
    """A deterministic offline provider.

    It exists so the full platform (ingest → retrieve → agent → metering) can be
    demonstrated and tested with no API keys and no network. It is deliberately
    simple: one knowledge-base lookup, then an answer assembled from the hits.
    """

    name = "mock"

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> Completion:
        tool_names = {t["function"]["name"] for t in (tools or [])}
        user_messages = [m for m in messages if m.get("role") == "user"]
        question = (user_messages[-1]["content"] if user_messages else "").strip()

        tool_results = [m for m in messages if m.get("role") == "tool"]
        prompt_tokens = _estimate_tokens(messages)

        if "knowledge_search" in tool_names and not tool_results:
            call_id = "call_" + hashlib.sha1(question.encode()).hexdigest()[:12]
            return Completion(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        name="knowledge_search",
                        arguments={"query": question or "overview", "k": 4},
                    )
                ],
                usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=12),
            )

        if tool_results:
            snippets = []
            for msg in tool_results:
                try:
                    payload = json.loads(msg.get("content") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                for hit in payload.get("results", [])[:2]:
                    text = hit.get("text", "").strip()
                    if text:
                        snippets.append(text[:320] + ("…" if len(text) > 320 else ""))

            if snippets:
                body = "\n\n".join(f"- {s}" for s in snippets if s)
                answer = (
                    "Here is what the knowledge base says:\n\n"
                    f"{body}\n\n"
                    "Sources are attached in the run transcript."
                )
            else:
                answer = (
                    "I could not find anything relevant in this workspace's knowledge base. "
                    "Add documents, then run the query again."
                )
            return Completion(
                content=answer,
                usage=Usage(
                    prompt_tokens=prompt_tokens + sum(_estimate_tokens([m]) for m in tool_results),
                    completion_tokens=_count(answer),
                ),
            )

        return Completion(
            content=(
                f"[mock provider] I received: {question!r}. "
                "Configure X0R_PROVIDER=openai_compatible and an API key for real answers."
            ),
            usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=24),
        )

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]] | None:
        """Signed feature hashing. Cheap, deterministic, dependency-free."""
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * _DIM
            for token in _tokens(text):
                digest = hashlib.md5(token.encode()).digest()
                idx = int.from_bytes(digest[:4], "big") % _DIM
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec))
            if norm:
                vec = [v / norm for v in vec]
            vectors.append(vec)
        return vectors


def _count(text: str) -> int:
    return max(1, len(text) // 4)


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += _count(content)
        elif content:
            total += _count(json.dumps(content))
        total += 4
    return total
