"""Core's entry point to the guard. The rules live in ``x0r_core.guard``.

This is an operator-controlled policy gate, not end-user RBAC.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .config import Settings
from .guard import (
    MAX_STRING_LENGTH,
    POLICY_VERSION,
    ActionRequest,
    Decision,
    check_action,
    check_arguments,
)

if TYPE_CHECKING:
    from .tools.base import Tool

__all__ = [
    "MAX_STRING_LENGTH",
    "POLICY_VERSION",
    "Decision",
    "authorize_tool",
    "check_arguments",
    "operator_tools",
]


def operator_tools(settings: Settings) -> set[str]:
    return {t.strip() for t in settings.operator_tool_allowlist.split(",") if t.strip()}


def authorize_tool(
    *, workspace_id: str, agent_workspace_id: str, agent_tools: list[str],
    tool_name: str, arguments: object, settings: Settings,
    registry: dict[str, Tool] | None = None,
) -> Decision:
    if registry is None:
        from .tools.builtin import REGISTRY as registry
    request = ActionRequest(
        workspace_id=workspace_id,
        agent_workspace_id=agent_workspace_id,
        agent_tools=agent_tools,
        tool_name=tool_name,
        arguments=arguments,
    )
    return check_action(
        request, operator_tools=operator_tools(settings), tools=registry, context=settings,
    )
