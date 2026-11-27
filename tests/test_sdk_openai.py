"""The SDK wrapper against the real openai client, with the network replaced."""
from __future__ import annotations

import json

import httpx
import pytest
from test_sdk_wrapper import TOOLS, _call, _reply

from x0r_core.guard import (
    Caller,
    DecisionRecord,
    Policy,
    ToolCallDenied,
    UnguardedCall,
    wrap,
)

openai = pytest.importorskip("openai")


def _transport(reply: dict, seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply)
    return httpx.MockTransport(handler)


def test_real_openai_client_allowed_and_denied():
    seen: list = []
    reply = _reply(_call("lookup_order", {"order_id": "A-1"}, "a"),
                   _call("refund_order", {"order_id": "A-1", "amount": 3}, "b"))
    inner = openai.OpenAI(api_key="test", base_url="http://llm.test/v1",
                          http_client=httpx.Client(transport=_transport(reply, seen)))
    records: list[DecisionRecord] = []
    client = wrap(inner, Policy(allow=["lookup_order"], on_decision=records.append,
                                on_denied="remove"))
    result = client.chat.completions.create(model="m", messages=[{"role": "user",
                                            "content": "hi"}], tools=TOOLS,
                                            x0r_caller=Caller("user-1"))
    assert isinstance(result, openai.types.chat.ChatCompletion)
    assert [c.id for c in result.choices[0].message.tool_calls] == ["a"]
    assert [(r.tool_call_id, r.allowed) for r in records] == [("a", True), ("b", False)]
    assert "x0r_caller" not in seen[0]
    assert seen[0]["tools"] == TOOLS


def test_real_openai_client_raise_mode():
    reply = _reply(_call("refund_order", {"order_id": "A-1", "amount": 3}))
    inner = openai.OpenAI(api_key="test", base_url="http://llm.test/v1",
                          http_client=httpx.Client(transport=_transport(reply, [])))
    client = wrap(inner, Policy(allow=["lookup_order"]))
    with pytest.raises(ToolCallDenied) as info:
        client.chat.completions.create(model="m", messages=[], tools=TOOLS)
    assert isinstance(info.value.response, openai.types.chat.ChatCompletion)
    assert info.value.response.choices[0].message.tool_calls is None


def _client(reply: dict, **kwargs) -> openai.OpenAI:
    return openai.OpenAI(api_key="test", base_url="http://llm.test/v1",
                         http_client=httpx.Client(transport=_transport(reply, [])), **kwargs)


def test_raw_response_header_on_the_client_fails_closed():
    reply = _reply(_call("refund_order", {"order_id": "A-1", "amount": 3}))
    seen: list = []
    inner = openai.OpenAI(api_key="test", base_url="http://llm.test/v1",
                          default_headers={"X-Stainless-Raw-Response": "true"},
                          http_client=httpx.Client(transport=_transport(reply, seen)))
    with pytest.raises(UnguardedCall):
        wrap(inner, Policy(allow=["lookup_order"]))
    with pytest.raises(UnguardedCall):
        wrap(_client(reply), Policy(allow=[])).with_options(
            default_headers={"x-stainless-raw-response": "true"})
    assert seen == []


def test_parse_error_carrying_the_completion_is_checked_too():
    strict = [{**t, "function": {**t["function"], "strict": True}} for t in TOOLS]
    reply = _reply(_call("refund_order", {"order_id": "A-1", "amount": 3}))
    cut = {"index": 1, "finish_reason": "length",
           "message": {"role": "assistant", "content": "partial"}}
    reply["choices"].append(cut)
    records: list[DecisionRecord] = []
    client = wrap(_client(reply), Policy(allow=["lookup_order"], on_denied="remove",
                                         on_decision=records.append))
    with pytest.raises(openai.LengthFinishReasonError) as info:
        client.chat.completions.parse(model="m", messages=[], tools=strict)
    assert info.value.completion.choices[0].message.tool_calls is None
    assert [r.reason for r in records] == ["not_granted_by_operator"]


def test_real_openai_with_options_stays_guarded():
    reply = _reply(_call("refund_order", {"order_id": "A-1", "amount": 3}))
    inner = openai.OpenAI(api_key="test", base_url="http://llm.test/v1",
                          http_client=httpx.Client(transport=_transport(reply, [])))
    client = wrap(inner, Policy(allow=["lookup_order"])).with_options(timeout=5)
    with pytest.raises(ToolCallDenied):
        client.chat.completions.create(model="m", messages=[], tools=TOOLS)


async def test_real_async_openai_client():
    reply = _reply(_call("refund_order", {"order_id": "A-1", "amount": 3}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply)

    inner = openai.AsyncOpenAI(
        api_key="test", base_url="http://llm.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = wrap(inner, Policy(allow=["lookup_order"]))
    with pytest.raises(ToolCallDenied):
        await client.chat.completions.create(model="m", messages=[], tools=TOOLS)


def test_example_blocks_someone_elses_refund():
    import subprocess
    import sys
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "examples" / "sdk_wrapper.py"
    out = subprocess.run([sys.executable, str(example)], capture_output=True, text=True,
                         check=True, timeout=60)
    assert '"reason": "not_callers_record"' in out.stdout
    assert "blocked: Tool call denied: refund_order (not_callers_record)" in out.stdout


def test_parse_validator_that_changes_a_value_is_denied():
    import pydantic

    class LookupOrder(pydantic.BaseModel):
        order_id: str

        @pydantic.field_validator("order_id")
        @classmethod
        def upper(cls, value: str) -> str:
            return value.upper()

    tool = openai.pydantic_function_tool(LookupOrder, name="lookup_order")
    records: list[DecisionRecord] = []
    for order_id, reason in [("A-1", "granted"), ("a-1", "parsed_arguments_changed")]:
        reply = _reply(_call("lookup_order", {"order_id": order_id}))
        client = wrap(_client(reply), Policy(allow=["lookup_order"], on_denied="remove",
                                             on_decision=records.append))
        client.chat.completions.parse(model="m", messages=[], tools=[tool])
        assert records[-1].reason == reason
