"""The guard's rules.

Every action passes the same checks, in order:

1. the call comes from the agent's own workspace;
2. the agent was granted the tool;
3. the operator granted the tool;
4. the arguments are an object;
5. the tool is known;
6. the arguments match the tool's declared ``parameters`` schema;
7. the tool's own ``authorize`` hook, if it has one, allows the call.

Step 6 enforces the subset of JSON Schema that tools declare: an object of named
properties with ``type`` (string, integer, number, boolean), ``required``,
``minLength``/``maxLength`` and ``minimum``/``maximum``. Undeclared arguments are
denied. Required strings must not be blank. Strings are capped at 8000 characters
unless the schema sets a lower ``maxLength``.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

POLICY_VERSION = "3"
MAX_STRING_LENGTH = 8000



@dataclass(frozen=True)
class Caller:
    """Who the action is for, as asserted by the authenticated app (e.g. its end user)."""

    subject: str
    attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HookContext:
    """What a tool's ``authorize`` hook gets besides the arguments.

    ``caller`` and ``tool_call_id`` come from the request, so a rule written once works in
    every connector. ``config`` is whatever the connector passes (Core passes its Settings).
    """

    caller: Caller | None
    tool_call_id: str | None
    config: Any

    def __getattr__(self, name: str) -> Any:
        # Hooks written before HookContext received the config object itself (Core's
        # Settings). Unknown attributes fall through to it so those hooks keep working.
        if name.startswith("__") or name == "config":
            raise AttributeError(name)
        return getattr(self.config, name)


# A tool-specific check run after the generic ones. Returns a denial reason, or None.
Authorizer = Callable[[dict[str, Any], HookContext], "str | None"]


class ToolPolicy(Protocol):
    """What the guard needs to know about a tool. Core's ``Tool`` fits this."""

    name: str
    parameters: dict[str, Any]
    authorize: Authorizer | None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ActionRequest:
    workspace_id: str
    agent_workspace_id: str
    agent_tools: Collection[str]
    tool_name: str
    arguments: object
    caller: Caller | None = None
    tool_call_id: str | None = None


# The only schema keywords the guard understands. A schema that uses any other keyword
# (pattern, format, anyOf, $ref, minProperties, ...) is denied, so a constraint the
# operator wrote is never skipped without anyone noticing.
PROPERTY_KEYWORDS = frozenset({
    "type", "minLength", "maxLength", "minimum", "maximum", "enum", "const",
    "description", "title", "default", "examples",
})
ROOT_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "description", "title",
    "$schema",
})


def _valid_value(value: Any, spec: dict[str, Any], *, required: bool) -> bool:
    if not isinstance(spec, dict) or spec.keys() - PROPERTY_KEYWORDS:
        return False
    if not _valid_type(value, spec, required=required):
        return False
    if "enum" in spec and not any(_same(value, option) for option in spec["enum"] or []):
        return False
    return "const" not in spec or _same(value, spec["const"])


def _same(value: Any, option: Any) -> bool:
    # JSON Schema: 1 equals 1.0, but true is not 1 (Python thinks it is).
    numbers = (int, float)
    if isinstance(value, bool) or isinstance(option, bool):
        return type(value) is type(option) and value == option
    if isinstance(value, numbers) and isinstance(option, numbers):
        return value == option
    return type(value) is type(option) and value == option


def _valid_type(value: Any, spec: dict[str, Any], *, required: bool) -> bool:
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
        # json.loads accepts NaN/Infinity, and NaN slips past every range check.
        ok = type(value) is int or (type(value) is float and math.isfinite(value))
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
    if parameters.keys() - ROOT_KEYWORDS:
        return "invalid_arguments"
    if parameters.get("type", "object") != "object":
        return "invalid_arguments"
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


def _is_text(value: object) -> bool:
    return isinstance(value, (str, bytes, bytearray))


_UNSET = object()


def check_action(
    request: ActionRequest,
    *,
    operator_tools: Collection[str],
    tools: Mapping[str, ToolPolicy],
    config: Any = _UNSET,
    context: Any = _UNSET,
) -> Decision:
    """Decide one action. The hook receives ``config`` or its legacy alias ``context``."""
    if context is not _UNSET:
        if config is not _UNSET:
            raise TypeError("check_action() accepts either config or context, not both")
        config = context
    elif config is _UNSET:
        config = None
    if request.workspace_id != request.agent_workspace_id:
        return Decision(False, "workspace_mismatch")
    # A plain string is a Collection too, and "in" would match substrings: deny it.
    if _is_text(request.agent_tools) or request.tool_name not in request.agent_tools:
        return Decision(False, "not_granted_to_agent")
    if _is_text(operator_tools) or request.tool_name not in operator_tools:
        return Decision(False, "not_granted_by_operator")
    arguments = request.arguments
    if not isinstance(arguments, dict):
        return Decision(False, "invalid_arguments")
    tool = tools.get(request.tool_name)
    # The policy found must be for the tool asked for, not another tool's rules.
    if tool is None or getattr(tool, "name", None) != request.tool_name:
        return Decision(False, "unknown_tool")
    reason = check_arguments(tool.parameters, arguments)
    if reason is not None:
        return Decision(False, reason)
    if tool.authorize is not None:
        try:
            hook = HookContext(
                caller=request.caller, tool_call_id=request.tool_call_id, config=config,
            )
            reason = tool.authorize(arguments, hook)
        except Exception:  # noqa: BLE001 - a failing hook denies, never allows
            reason = "authorizer_error"
        if reason is not None:
            return Decision(False, str(reason))
    return Decision(True, "granted")
