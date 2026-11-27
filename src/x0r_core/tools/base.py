from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..guard import Authorizer


@dataclass
class ToolContext:
    session: AsyncSession
    workspace_id: str
    settings: Settings


Handler = Callable[..., Awaitable[Any]]
# ``authorize`` is an extra, tool-specific policy check run after the generic argument
# checks. It gets the arguments and a guard HookContext (caller, tool_call_id, and Core's
# Settings as ``config``); it returns a denial reason, or None to allow.


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    authorize: Authorizer | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
