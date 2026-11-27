"""The SDK wrapper: tool calls from an OpenAI-style client are checked before the app sees them."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from x0r_core.guard import (
    Caller,
    DecisionNotRecorded,
    DecisionRecord,
    HookContext,
    Policy,
    ToolCallDenied,
    UnguardedAttribute,
    UnguardedCall,
    wrap,
)

TOOLS = [
    {"type": "function", "function": {
        "name": "lookup_order",
        "parameters": {"type": "object",
                       "properties": {"order_id": {"type": "string", "maxLength": 32}},
                       "required": ["order_id"]},
    }},
    {"type": "function", "function": {
        "name": "refund_order",
        "parameters": {"type": "object",
                       "properties": {"order_id": {"type": "string"},
                                      "amount": {"type": "number", "minimum": 0}},
                       "required": ["order_id", "amount"]},
    }},
]


def _call(name: str, args: Any, call_id: str = "call_1") -> dict:
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


def _reply(*calls: dict, content: str | None = None) -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                     "message": {"role": "assistant", "content": content,
                                 "tool_calls": list(calls) or None}}],
    }


class DictClient:
    """A client with the OpenAI shape that returns plain dicts."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.sent: list[dict] = []
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.sent.append(kwargs)
                return copy.deepcopy(outer.reply)

            parse = create

        class Chat:
            completions = Completions()

        self.chat = Chat()
        self.models = "models-api"


def _wrapped(reply: dict, **policy: Any):
    records: list[DecisionRecord] = []
    policy.setdefault("allow", ["lookup_order", "refund_order"])
    policy.setdefault("on_decision", records.append)
    inner = DictClient(reply)
    return wrap(inner, Policy(**policy)), inner, records


def _create(client, **kwargs):
    kwargs.setdefault("model", "m")
    kwargs.setdefault("messages", [{"role": "user", "content": "hi"}])
    kwargs.setdefault("tools", TOOLS)
    return client.chat.completions.create(**kwargs)


# ── allow / deny ─────────────────────────────────────────────────────────────

def test_allowed_call_passes_through_and_is_recorded():
    client, inner, records = _wrapped(_reply(_call("lookup_order", {"order_id": "A-1"})))
    reply = _create(client)
    assert reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "lookup_order"
    assert [(r.tool, r.allowed, r.reason, r.tool_call_id) for r in records] == [
        ("lookup_order", True, "granted", "call_1")
    ]
    assert "x0r_caller" not in inner.sent[0]


def test_plain_answer_needs_no_decision():
    client, _, records = _wrapped(_reply(content="hello"))
    assert _create(client)["choices"][0]["message"]["content"] == "hello"
    assert records == []


@pytest.mark.parametrize("call,reason", [
    (_call("refund_order", {"order_id": "A-1", "amount": 5}), "not_granted_by_operator"),
    (_call("delete_everything", {}), "not_granted_to_agent"),
    (_call("lookup_order", {"order_id": "x" * 33}), "invalid_arguments"),
    (_call("lookup_order", {"order_id": "A-1", "extra": 1}), "unexpected_arguments"),
    (_call("lookup_order", "{not json"), "invalid_arguments"),
    (_call("lookup_order", "[1, 2]"), "invalid_arguments"),
    (_call("refund_order", '{"order_id": "A-1", "amount": NaN}'), "invalid_arguments"),
])
def test_denied_calls_raise(call, reason):
    allow = ["lookup_order"] if reason == "not_granted_by_operator" else None
    client, _, records = _wrapped(_reply(call), **({"allow": allow} if allow else {}))
    with pytest.raises(ToolCallDenied) as info:
        _create(client)
    assert [d.reason for d in info.value.denied] == [reason]
    assert [r.reason for r in records] == [reason]


def test_tool_not_offered_in_request_is_denied_even_if_operator_allows():
    client, _, _ = _wrapped(_reply(_call("refund_order", {"order_id": "A-1", "amount": 1})))
    with pytest.raises(ToolCallDenied) as info:
        _create(client, tools=TOOLS[:1])
    assert info.value.denied[0].reason == "not_granted_to_agent"


def test_remove_mode_drops_only_denied_calls():
    reply = _reply(_call("lookup_order", {"order_id": "A-1"}, "ok"),
                   _call("refund_order", {"order_id": "A-1", "amount": 9}, "bad"))
    client, _, records = _wrapped(reply, allow=["lookup_order"], on_denied="remove")
    calls = _create(client)["choices"][0]["message"]["tool_calls"]
    assert [c["id"] for c in calls] == ["ok"]
    assert [(r.tool_call_id, r.allowed) for r in records] == [("ok", True), ("bad", False)]


def test_remove_mode_with_nothing_left_clears_tool_calls():
    reply = _reply(_call("refund_order", {"order_id": "A-1", "amount": 9}))
    client, _, _ = _wrapped(reply, allow=["lookup_order"], on_denied="remove")
    assert _create(client)["choices"][0]["message"]["tool_calls"] is None


def test_unsupported_tool_type_is_denied():
    reply = _reply({"id": "c", "type": "custom", "custom": {"name": "x", "input": "y"}})
    client, _, _ = _wrapped(reply)
    with pytest.raises(ToolCallDenied) as info:
        _create(client)
    assert info.value.denied[0].reason == "unsupported_tool_type"


def test_legacy_function_call_in_reply_is_denied():
    reply = _reply()
    reply["choices"][0]["message"]["function_call"] = {"name": "lookup_order",
                                                       "arguments": "{}"}
    client, _, _ = _wrapped(reply)
    with pytest.raises(ToolCallDenied):
        _create(client)


# ── caller and rules ─────────────────────────────────────────────────────────

def own_order(arguments: dict, ctx: HookContext) -> str | None:
    if ctx.caller is None:
        return "caller_required"
    if arguments["order_id"] not in ctx.caller.attributes.get("orders", "").split(","):
        return "not_callers_record"
    return None


def test_rule_sees_the_caller():
    reply = _reply(_call("lookup_order", {"order_id": "A-1"}))
    client, _, records = _wrapped(reply, rules={"lookup_order": own_order})
    mine = Caller("user-1", {"orders": "A-1,A-2"})
    assert _create(client, x0r_caller=mine)
    assert records[-1].caller_subject == "user-1"
    with pytest.raises(ToolCallDenied) as info:
        _create(client, x0r_caller=Caller("user-2", {"orders": "B-9"}))
    assert info.value.denied[0].reason == "not_callers_record"
    with pytest.raises(ToolCallDenied) as info:
        _create(client)
    assert info.value.denied[0].reason == "caller_required"


def test_default_caller_and_per_call_override():
    records: list[DecisionRecord] = []
    inner = DictClient(_reply(_call("lookup_order", {"order_id": "A-1"})))
    client = wrap(inner, Policy(allow=["lookup_order"], on_decision=records.append),
                  caller=Caller("default"))
    _create(client)
    _create(client, x0r_caller=Caller("override"))
    assert [r.caller_subject for r in records] == ["default", "override"]


def test_rule_config_is_passed():
    seen = []

    def rule(arguments, ctx):
        seen.append(ctx.config)

    reply = _reply(_call("lookup_order", {"order_id": "A-1"}))
    client, _, _ = _wrapped(reply, rules={"lookup_order": rule}, config={"env": "prod"})
    _create(client)
    assert seen == [{"env": "prod"}]


# ── fail closed ──────────────────────────────────────────────────────────────

def test_failed_recording_withholds_the_reply():
    def broken(record):
        raise OSError("disk full")

    client, _, _ = _wrapped(_reply(_call("lookup_order", {"order_id": "A-1"})),
                            on_decision=broken)
    with pytest.raises(DecisionNotRecorded):
        _create(client)


@pytest.mark.parametrize("kwargs", [
    {"stream": True},
    {"functions": [{"name": "f"}]},
    {"function_call": "auto"},
])
def test_unguarded_request_options_are_refused(kwargs):
    client, inner, _ = _wrapped(_reply())
    with pytest.raises(UnguardedCall):
        _create(client, **kwargs)
    assert inner.sent == []


@pytest.mark.parametrize("path", ["responses", "beta", "with_raw_response",
                                  "with_streaming_response"])
def test_unguarded_client_paths_are_refused(path):
    client, _, _ = _wrapped(_reply())
    with pytest.raises(UnguardedAttribute):
        getattr(client, path)
    assert not hasattr(client, path)


def test_unguarded_completion_methods_are_refused():
    client, _, _ = _wrapped(_reply())
    with pytest.raises(UnguardedAttribute):
        client.chat.completions.stream  # noqa: B018
    with pytest.raises(UnguardedAttribute):
        client.chat.something  # noqa: B018


def test_other_client_attributes_pass_through():
    client, _, _ = _wrapped(_reply())
    assert client.models == "models-api"


def test_policy_rejects_a_string_allow_list():
    with pytest.raises(TypeError):
        Policy(allow="lookup_order")
    with pytest.raises(ValueError):
        Policy(allow=[], on_denied="ignore")


def test_wrap_needs_a_policy():
    with pytest.raises(TypeError):
        wrap(DictClient(_reply()), {"allow": []})


# ── review fixes: ways around the wrapper stay closed ────────────────────────

def test_raise_mode_exception_holds_no_denied_calls():
    reply = _reply(_call("lookup_order", {"order_id": "A-1"}, "ok"),
                   _call("refund_order", {"order_id": "A-1", "amount": 9}, "bad"))
    client, _, _ = _wrapped(reply, allow=["lookup_order"])
    with pytest.raises(ToolCallDenied) as info:
        _create(client)
    calls = info.value.response["choices"][0]["message"]["tool_calls"]
    assert [c["id"] for c in calls] == ["ok"]


def test_finish_reason_is_reset_when_every_call_is_removed():
    reply = _reply(_call("refund_order", {"order_id": "A-1", "amount": 9}))
    client, _, _ = _wrapped(reply, allow=["lookup_order"], on_denied="remove")
    assert _create(client)["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("calls,reasons", [
    ([_call("lookup_order", {"order_id": "A-1"}, "")], ["missing_tool_call_id"]),
    ([_call("lookup_order", {"order_id": "A-1"}, "x"),
      _call("lookup_order", {"order_id": "A-2"}, "x")], ["granted", "duplicate_tool_call_id"]),
    ([_call("lookup_order", "   ")], ["invalid_arguments"]),
])
def test_call_ids_and_blank_arguments(calls, reasons):
    client, _, records = _wrapped(_reply(*calls), on_denied="remove")
    _create(client)
    assert [r.reason for r in records] == reasons


def test_reply_that_is_not_a_completion_is_refused():
    client, _, records = _wrapped("<raw http response>")  # type: ignore[arg-type]
    with pytest.raises(UnguardedCall):
        _create(client)
    assert records == []


@pytest.mark.parametrize("kwargs", [
    {"extra_body": {"tools": TOOLS}},
    {"extra_body": {"stream": True}},
    {"extra_body": {"n": 2}},
    {"extra_body": "tools"},
    {"extra_headers": {"X-Stainless-Raw-Response": "true"}},
])
def test_hidden_request_options_are_refused(kwargs):
    client, inner, _ = _wrapped(_reply())
    with pytest.raises(UnguardedCall):
        _create(client, **kwargs)
    assert inner.sent == []


@pytest.mark.parametrize("path", ["post", "get", "request", "completions", "_client_attr"])
def test_low_level_client_paths_are_refused(path):
    client, _, _ = _wrapped(_reply())
    with pytest.raises(UnguardedAttribute):
        getattr(client, path)


def test_policy_is_a_snapshot():
    allow = ["lookup_order"]
    reply = _reply(_call("refund_order", {"order_id": "A-1", "amount": 1}))
    client, _, _ = _wrapped(reply, allow=allow)
    allow.append("refund_order")
    with pytest.raises(ToolCallDenied):
        _create(client)


def test_async_on_decision_is_rejected():
    async def save(record):
        pass

    with pytest.raises(TypeError):
        Policy(allow=[], on_decision=save)

    async def later():
        pass

    client, _, _ = _wrapped(_reply(_call("lookup_order", {"order_id": "A-1"})),
                            on_decision=lambda record: later())
    with pytest.raises(DecisionNotRecorded):
        _create(client)
