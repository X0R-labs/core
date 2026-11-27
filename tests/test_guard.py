"""The guard on its own: no Core settings, tools or database."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest

from x0r_core.guard import ActionRequest, Caller, Decision, HookContext, check_action


@dataclass
class Spec:
    name: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    authorize: Any = None


REFUND = Spec(
    name="refund",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string", "maxLength": 32},
                       "amount": {"type": "number", "minimum": 0, "maximum": 100}},
        "required": ["order_id", "amount"],
    },
)


def _request(**overrides: Any) -> ActionRequest:
    values = dict(workspace_id="w1", agent_workspace_id="w1", agent_tools=["refund"],
                  tool_name="refund", arguments={"order_id": "A1", "amount": 10})
    values.update(overrides)
    return ActionRequest(**values)


def _check(request: ActionRequest, **kwargs: Any) -> Decision:
    kwargs.setdefault("operator_tools", {"refund"})
    kwargs.setdefault("tools", {"refund": REFUND})
    return check_action(request, **kwargs)


def test_allows_a_granted_valid_call():
    assert _check(_request()) == Decision(True, "granted")


def test_checks_run_in_order():
    assert _check(_request(workspace_id="w2", agent_tools=[])).reason == "workspace_mismatch"
    assert _check(_request(agent_tools=[]), operator_tools=set()).reason == "not_granted_to_agent"
    assert _check(_request(), operator_tools=set()).reason == "not_granted_by_operator"
    assert _check(_request(arguments=[1]), tools={}).reason == "invalid_arguments"
    assert _check(_request(), tools={}).reason == "unknown_tool"


def test_string_grant_lists_are_denied():
    # "fund" is a substring of "refund"; a string must never act as a list of grants.
    fund = Spec(name="fund", parameters=REFUND.parameters)
    request = _request(tool_name="fund", agent_tools="refund")
    tools = {"fund": fund}
    assert _check(request, operator_tools={"fund"}, tools=tools).reason == "not_granted_to_agent"
    request = _request(tool_name="fund", agent_tools=["fund"])
    assert _check(request, operator_tools="refund", tools=tools).reason == "not_granted_by_operator"


def test_policy_for_another_tool_is_denied():
    other = Spec(name="calculator", parameters=REFUND.parameters)
    assert _check(_request(), tools={"refund": other}).reason == "unknown_tool"


def test_schema_limits_apply():
    for amount in (500, float("nan"), float("inf"), True):
        args = {"order_id": "A1", "amount": amount}
        assert _check(_request(arguments=args)).reason == "invalid_arguments"
    extra = {"order_id": "A1", "amount": 1, "note": "x"}
    assert _check(_request(arguments=extra)).reason == "unexpected_arguments"


def test_hook_gets_caller_call_id_and_config():
    seen: list[HookContext] = []

    def only_small(arguments: dict[str, Any], hook: HookContext) -> str | None:
        seen.append(hook)
        return "over_limit" if arguments["amount"] > hook.config["max"] else None

    tool = Spec(name="refund", parameters=REFUND.parameters, authorize=only_small)
    caller = Caller(subject="user-1", attributes={"role": "agent"})
    request = _request(caller=caller, tool_call_id="call_9")
    config = {"max": 5}
    assert _check(request, tools={"refund": tool}, config=config).reason == "over_limit"
    assert seen == [HookContext(caller=caller, tool_call_id="call_9", config=config)]


def test_context_is_a_keyword_alias_for_config():
    config = object()

    def authorize(arguments, hook):
        return None if hook.config is config else "wrong_config"

    tool = Spec(name="refund", parameters=REFUND.parameters, authorize=authorize)
    assert _check(_request(), tools={"refund": tool}, context=config).allowed
    with pytest.raises(TypeError, match="config or context"):
        _check(_request(), config=None, context=None)


def test_own_record_rule():
    # The CRM case: a caller may only touch their own customer record.
    def own_record(arguments: dict[str, Any], hook: HookContext) -> str | None:
        if hook.caller is None or arguments["order_id"] != hook.caller.subject:
            return "not_callers_record"
        return None

    tool = Spec(name="refund", parameters=REFUND.parameters, authorize=own_record)
    tools = {"refund": tool}
    mine = _request(caller=Caller(subject="A1"))
    theirs = _request(caller=Caller(subject="B2"))
    assert _check(mine, tools=tools).allowed
    assert _check(theirs, tools=tools).reason == "not_callers_record"
    assert _check(_request(), tools=tools).reason == "not_callers_record"


def test_a_failing_hook_denies():
    def broken(arguments: dict[str, Any], context: Any) -> str | None:
        raise RuntimeError("boom")

    tool = Spec(name="refund", parameters=REFUND.parameters, authorize=broken)
    assert _check(_request(), tools={"refund": tool}).reason == "authorizer_error"


def test_guard_imports_only_the_standard_library():
    code = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "import x0r_guard\n"
        "new = sorted(set(sys.modules) - before)\n"
        "print(json.dumps(new))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    new = json.loads(out.stdout)
    ours = [m for m in new if m.split(".")[0] == "x0r_guard"]
    outside = [m for m in new if m not in ours and m.split(".")[0] not in sys.stdlib_module_names]
    assert {"x0r_guard", "x0r_guard.engine"} <= set(ours)
    assert outside == []


@pytest.mark.parametrize("spec,value,ok", [
    ({"type": "string", "enum": ["a", "b"]}, "a", True),
    ({"type": "string", "enum": ["a", "b"]}, "c", False),
    ({"type": "integer", "enum": [1, 2]}, True, False),
    ({"type": "string", "const": "x"}, "x", True),
    ({"type": "string", "const": "x"}, "y", False),
    ({"type": "string", "pattern": "^a"}, "abc", False),
    ({"type": "number", "exclusiveMaximum": 10}, 1, False),
    ({"type": "string", "format": "email"}, "a@b.c", False),
    ({"type": "string", "$dynamicRef": "#x"}, "a", False),
    ({"type": "number", "const": 1}, 1.0, True),
    ({"type": "number", "enum": [1.5, 2]}, 2.0, True),
    ({"type": "boolean", "const": True}, 1, False),
])
def test_enum_const_and_unsupported_keywords(spec, value, ok):
    from x0r_core.guard.engine import check_arguments

    parameters = {"type": "object", "properties": {"v": spec}, "required": ["v"]}
    assert (check_arguments(parameters, {"v": value}) is None) is ok


@pytest.mark.parametrize("root", [
    {"type": "object", "properties": {"v": {"type": "string"}}, "minProperties": 2},
    {"type": "object", "properties": {"v": {"type": "string"}}, "anyOf": [{}]},
    {"type": "array", "properties": {"v": {"type": "string"}}},
])
def test_root_keywords_outside_the_allowlist_deny(root):
    from x0r_core.guard.engine import check_arguments

    assert check_arguments(root, {"v": "a"}) == "invalid_arguments"


@pytest.mark.parametrize("value, ok", [
    ([1, 2.0], True),
    (["1", 2], False),
    ([1, True], False),
    ([1], False),
    ([1, 2, 3, 4], False),
    ([1, 1.0], False),
    ("12", False),
])
def test_array_constraints(value, ok):
    from x0r_guard.engine import check_arguments

    schema = {"properties": {"values": {
        "type": "array", "items": {"type": "number"},
        "minItems": 2, "maxItems": 3, "uniqueItems": True,
    }}}
    assert (check_arguments(schema, {"values": value}) is None) is ok


def test_nested_objects_and_array_equality():
    from x0r_guard.engine import check_arguments

    item = {
        "type": "object", "properties": {"amount": {"type": "number"}},
        "required": ["amount"], "additionalProperties": True,
    }
    schema = {"properties": {"orders": {
        "type": "array", "items": item, "uniqueItems": True,
        "enum": [[{"amount": 1}, {"amount": 2}], [{"amount": True}]],
    }}}
    assert check_arguments(schema, {"orders": [{"amount": 1.0}, {"amount": 2}]}) is None
    for orders in (
        [{"amount": 1, "extra": 2}], [{}], [{"amount": True}], [{"amount": 1}, {"amount": 1.0}],
    ):
        assert check_arguments(schema, {"orders": orders}) == "invalid_arguments"

    # The item schema accepts a numeric value, but the enum contains a boolean.
    assert check_arguments(schema, {"orders": [{"amount": 1}]}) == "invalid_arguments"


@pytest.mark.parametrize("definitions", ["$defs", "definitions"])
def test_local_references(definitions):
    from x0r_guard.engine import check_arguments, schema_supported

    schema = {
        "$ref": f"#/{definitions}/Arguments",
        definitions: {
            "Arguments": {
                "type": "object", "properties": {"order": {"$ref": f"#/{definitions}/Order"}},
            },
            "Order": {"type": "string", "minLength": 2},
        },
    }
    assert schema_supported(schema)
    assert check_arguments(schema, {"order": "A1"}) is None
    assert check_arguments(schema, {"order": "A"}) == "invalid_arguments"


@pytest.mark.parametrize("spec", [
    {"$ref": "#/$defs/Loop"},
    {"$ref": "#/$defs/Missing"},
    {"$ref": "https://example.org/schema"},
    {"type": "string", "pattern": "^a"},
    {"type": "array", "items": {"type": "number"}, "minItems": True},
    {"type": "array", "items": {"type": "number"}, "maxItems": -1},
    {"type": "array", "items": {"type": "number"}, "uniqueItems": 1},
])
def test_unsupported_optional_schema_is_denied(spec):
    from x0r_guard.engine import check_arguments, schema_supported

    schema = {"properties": {"v": spec}, "$defs": {"Loop": {"$ref": "#/$defs/Loop"}}}
    assert not schema_supported(schema)
    assert check_arguments(schema, {}) == "invalid_arguments"


def test_schema_depth_limit():
    from x0r_guard.engine import MAX_SCHEMA_DEPTH, check_arguments, schema_supported

    schema = {"type": "string"}
    arguments = "value"
    for _ in range(MAX_SCHEMA_DEPTH):
        schema = {"type": "object", "properties": {"v": schema}}
        arguments = {"v": arguments}
    assert schema_supported(schema)
    assert check_arguments(schema, arguments) is None
    deeper = {"type": "object", "properties": {"v": schema}}
    assert not schema_supported(deeper)
    assert check_arguments(deeper, {"v": arguments}) == "invalid_arguments"
