"""The guard's rules.

Every action passes the same checks, in order:

1. the call comes from the agent's own workspace;
2. the agent was granted the tool;
3. the operator granted the tool;
4. the arguments are an object;
5. the tool is known;
6. the arguments match the tool's declared ``parameters`` schema;
7. the tool's own ``authorize`` hook, if it has one, allows the call.

Step 6 enforces a bounded subset of JSON Schema. Undeclared properties are
denied even when additionalProperties is true. Required strings must not be blank.
Strings are capped at 8000 characters unless the schema sets a lower maxLength.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

POLICY_VERSION = "4"
MAX_STRING_LENGTH = 8000
MAX_SCHEMA_DEPTH = 5


@dataclass(frozen=True)
class Caller:
    """Who the action is for, as asserted by the authenticated app (e.g. its end user).

    Attribute values are strings or tuples of strings, e.g. ``{"orders": ("A-1", "A-2")}``.
    A rule checks a tuple by exact membership; it never splits a string on commas.
    """

    subject: str
    attributes: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict)


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


COMMON_KEYWORDS = {"type", "description", "title"}
OBJECT_KEYWORDS = {"properties", "required", "additionalProperties"}
DEFINITION_KEYWORDS = {"$defs", "definitions"}
ROOT_KEYWORDS = frozenset(COMMON_KEYWORDS | OBJECT_KEYWORDS | DEFINITION_KEYWORDS | {
    "$schema", "$ref", "enum", "const",
})
TYPE_KEYWORDS = {
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum"},
    "number": {"minimum", "maximum"},
    "boolean": set(),
    "array": {"items", "minItems", "maxItems", "uniqueItems"},
    "object": OBJECT_KEYWORDS,
}


def _reference(spec: dict, root: dict) -> dict | None:
    ref = spec["$ref"]
    if not isinstance(ref, str):
        return None
    parts = ref.split("/")
    if len(parts) != 3 or parts[0] != "#" or parts[1] not in DEFINITION_KEYWORDS:
        return None
    name = parts[2]
    if not name or "%" in name or re.search(r"~(?![01])", name):
        return None
    name = name.replace("~1", "/").replace("~0", "~")
    target = root.get(parts[1], {}).get(name)
    return target if isinstance(target, dict) else None


def _supported(spec: object, root: dict, depth: int, *, object_root: bool = False) -> bool:
    if depth > MAX_SCHEMA_DEPTH or not isinstance(spec, dict):
        return False
    if "$ref" in spec:
        metadata = {"description", "title"}
        if spec is root:
            metadata |= DEFINITION_KEYWORDS | {"$schema"}
        if spec.keys() - metadata - {"$ref"}:
            return False
        return _supported(_reference(spec, root), root, depth + 1, object_root=object_root)

    kind = spec.get("type", "object" if spec is root else None)
    if not isinstance(kind, str) or kind not in TYPE_KEYWORDS:
        return False
    if object_root and kind != "object":
        return False
    keywords = ROOT_KEYWORDS if spec is root else (
        COMMON_KEYWORDS | TYPE_KEYWORDS[kind] | {"default", "examples", "enum", "const"}
    )
    if spec.keys() - keywords:
        return False
    if "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
        return False
    for keyword in spec.keys() & {"minLength", "maxLength", "minItems", "maxItems"}:
        if type(spec[keyword]) is not int or spec[keyword] < 0:
            return False
    for keyword in spec.keys() & {"minimum", "maximum"}:
        value = spec[keyword]
        if type(value) is not int and (type(value) is not float or not math.isfinite(value)):
            return False
    if kind == "array":
        return (type(spec.get("uniqueItems", False)) is bool
                and _supported(spec.get("items"), root, depth + 1))
    if kind == "object":
        properties = spec.get("properties", {})
        required = spec.get("required", [])
        return (
            type(spec.get("additionalProperties", False)) is bool
            and isinstance(properties, dict)
            and isinstance(required, list)
            and all(isinstance(name, str) and name in properties for name in required)
            and all(isinstance(name, str) and _supported(value, root, depth + 1)
                    for name, value in properties.items())
        )
    return True


def schema_supported(schema: object) -> bool:
    """Whether this tool's complete argument schema can be enforced."""
    return (
        isinstance(schema, dict)
        and all(isinstance(schema.get(key, {}), dict) for key in DEFINITION_KEYWORDS)
        and _supported(schema, schema, 0, object_root=True)
    )


def _resolved(spec: dict, root: dict) -> dict:
    # Support checking has already bounded the reference chain.
    while "$ref" in spec:
        spec = _reference(spec, root)
    return spec


def _same(value: Any, option: Any) -> bool:
    if type(value) in (int, float) and type(option) in (int, float):
        return value == option
    if type(value) is not type(option):
        return False
    if isinstance(value, list):
        return len(value) == len(option) and all(
            _same(a, b) for a, b in zip(value, option, strict=True)
        )
    if isinstance(value, dict):
        return value.keys() == option.keys() and all(
            _same(item, option[key]) for key, item in value.items()
        )
    return value == option


def _equality_key(value: Any) -> Any:
    # Tag booleans separately from numbers, including inside arrays and objects.
    if isinstance(value, list):
        return (list, tuple(_equality_key(item) for item in value))
    if isinstance(value, dict):
        return (dict, frozenset((key, _equality_key(item)) for key, item in value.items()))
    if type(value) in (int, float):
        # Python hashes equal ints and floats alike. Keep large ints at full precision.
        return (int, value)
    return (type(value), value)


def _valid_value(value: Any, spec: dict, root: dict, *, required: bool = False) -> bool:
    spec = _resolved(spec, root)
    kind = spec.get("type", "object" if spec is root else None)
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = spec.get("properties", {})
        names = set(spec.get("required", []))
        if names - value.keys() or value.keys() - properties.keys():
            return False
        if not all(_valid_value(item, properties[name], root, required=name in names)
                   for name, item in value.items()):
            return False
    elif kind == "array":
        if not isinstance(value, list) or len(value) < spec.get("minItems", 0):
            return False
        if len(value) > spec.get("maxItems", len(value)):
            return False
        if not all(_valid_value(item, spec["items"], root) for item in value):
            return False
        if spec.get("uniqueItems") and len({_equality_key(item) for item in value}) != len(value):
            return False
    elif not _valid_type(value, spec, required=required):
        return False
    if "enum" in spec and not any(_same(value, option) for option in spec["enum"]):
        return False
    return "const" not in spec or _same(value, spec["const"])


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
    if not schema_supported(parameters):
        return "invalid_arguments"
    spec = _resolved(parameters, parameters)
    extra = arguments.keys() - spec.get("properties", {}).keys()
    declared = {name: value for name, value in arguments.items() if name not in extra}
    if not _valid_value(declared, parameters, parameters):
        return "invalid_arguments"
    return "unexpected_arguments" if extra else None


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
