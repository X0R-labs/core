"""Small operator-controlled policy gate; this is not end-user RBAC."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .config import Settings


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def authorize_tool(
    *, workspace_id: str, agent_workspace_id: str, agent_tools: list[str],
    tool_name: str, arguments: object, settings: Settings,
) -> Decision:
    if workspace_id != agent_workspace_id:
        return Decision(False, "workspace_mismatch")
    if tool_name not in agent_tools:
        return Decision(False, "not_granted_to_agent")
    operator_tools = {t.strip() for t in settings.operator_tool_allowlist.split(",") if t.strip()}
    if tool_name not in operator_tools:
        return Decision(False, "not_granted_by_operator")
    if not isinstance(arguments, dict):
        return Decision(False, "invalid_arguments")
    required = {"knowledge_search": "query", "calculator": "expression", "http_get": "url"}
    field = required.get(tool_name)
    if field is None:
        return Decision(False, "unknown_tool")
    allowed_fields = {field, "k"} if tool_name == "knowledge_search" else {field}
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 8000:
        return Decision(False, "invalid_arguments")
    if set(arguments) - allowed_fields:
        return Decision(False, "unexpected_arguments")
    if "k" in arguments and (
        type(arguments["k"]) is not int or not 1 <= arguments["k"] <= 10
    ):
        return Decision(False, "invalid_arguments")
    if tool_name == "http_get":
        try:
            url = urlparse(value)
            if (
                url.scheme not in {"http", "https"}
                or url.hostname not in settings.allowed_http_hosts
                or url.username is not None
                or url.password is not None
                or url.port not in {None, 80, 443}
            ):
                return Decision(False, "http_destination_denied")
        except ValueError:
            return Decision(False, "invalid_arguments")
    return Decision(True, "granted")
