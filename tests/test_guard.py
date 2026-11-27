"""The guard on its own: no Core settings, tools or database."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

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
        "import x0r_core.guard\n"
        "new = sorted(set(sys.modules) - before)\n"
        "print(json.dumps(new))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    new = json.loads(out.stdout)
    ours = [m for m in new if m.split(".")[0] == "x0r_core"]
    outside = [m for m in new if m not in ours and m.split(".")[0] not in sys.stdlib_module_names]
    assert {"x0r_core.guard", "x0r_core.guard.engine"} <= set(ours)
    assert all(m == "x0r_core" or m.startswith("x0r_core.guard") for m in ours)
    assert outside == []
