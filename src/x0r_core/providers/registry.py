from __future__ import annotations

from ..config import get_settings
from .base import Completion, Provider
from .mock import MockProvider
from .openai_compat import OpenAICompatibleProvider

_REGISTRY: dict[str, type[Provider]] = {
    "mock": MockProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "openai": OpenAICompatibleProvider,
}


def build_provider(name: str | None = None) -> Provider:
    key = (name or get_settings().provider).strip().lower()
    try:
        return _REGISTRY[key]()
    except KeyError as exc:
        raise ValueError(
            f"Unknown provider {key!r}. Choose one of: {', '.join(sorted(_REGISTRY))}"
        ) from exc


__all__ = ["Completion", "Provider", "build_provider"]
