"""Small operator-controlled policy gate; this is not end-user RBAC.

Every tool call passes the same checks, in order:

1. the call comes from the agent's own workspace;
2. the agent was granted the tool;
3. the operator allowlist (``X0R_OPERATOR_TOOL_ALLOWLIST``) grants the tool;
4. the tool is registered;
5. the arguments match the tool's declared ``parameters`` schema;
6. the tool's own ``authorize`` hook, if it has one, allows the call.

Step 5 enforces the subset of JSON Schema that tools declare: an object of named
properties with ``type`` (string, integer, number, boolean), ``required``,
``minLength``/``maxLength`` and ``minimum``/``maximum``. Undeclared arguments are
denied. Required strings must not be blank. Strings are capped at 8000 characters
unless the schema sets a lower ``maxLength``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config import Settings

if TYPE_CHECKING:
    from .tools.base import Tool

POLICY_VERSION = "2"
MAX_STRING_LENGTH = 8000


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _operator_tools(settings: Settings) -> set[str]:
    return {t.strip() for t in settings.operator_tool_allowlist.split(",") if t.strip()}


def _valid_value(value: Any, spec: dict[str, Any], *, required: bool) -> bool:
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str):
            return False
        if required and not value.strip():
            return False
        limit = min(spec.get("maxLength", MAX_STRING_LENGTH), MAX_STRING_LENGTH)
        return spec.get("minLength", 0) <= len(value) <= limit
    if kind == "integer":
        ok = type(value) is int
    elif kind == "number":
        ok = type(value) in (int, float)
    elif kind == "boolean":
        return type(value) is bool
    else:
        # Undeclared or unsupported types are denied rather than passed through.
        return False
    if not ok:
        return False
    if "minimum" in spec and value < spec["minimum"]:
        return False
    if "maximum" in spec and value > spec["maximum"]:
        return False
    return True


def check_arguments(parameters: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Return a denial reason if ``arguments`` do not match ``parameters``."""
    properties: dict[str, Any] = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    for name in required:
        if name not in arguments:
            return "invalid_arguments"
    for name, value in arguments.items():
        if name not in properties:
            continue
        if not _valid_value(value, properties[name], required=name in required):
            return "invalid_arguments"
    if set(arguments) - set(properties):
        return "unexpected_arguments"
    return None


def authorize_tool(
    *, workspace_id: str, agent_workspace_id: str, agent_tools: list[str],
    tool_name: str, arguments: object, settings: Settings,
    registry: dict[str, Tool] | None = None,
) -> Decision:
    if workspace_id != agent_workspace_id:
        return Decision(False, "workspace_mismatch")
    if tool_name not in agent_tools:
        return Decision(False, "not_granted_to_agent")
    if tool_name not in _operator_tools(settings):
        return Decision(False, "not_granted_by_operator")
    if not isinstance(arguments, dict):
        return Decision(False, "invalid_arguments")
    if registry is None:
        from .tools.builtin import REGISTRY as registry
    tool = registry.get(tool_name)
    if tool is None:
        return Decision(False, "unknown_tool")
    reason = check_arguments(tool.parameters, arguments)
    if reason is not None:
        return Decision(False, reason)
    if tool.authorize is not None:
        try:
            reason = tool.authorize(arguments, settings)
        except Exception:  # noqa: BLE001 - a failing hook denies, never allows
            reason = "authorizer_error"
        if reason is not None:
            return Decision(False, reason)
    return Decision(True, "granted")
