"""Guard your own OpenAI app in one line: no Core server needed.

    pip install -e . openai
    python examples/sdk_wrapper.py            # offline: a fake model asks for a refund
    OPENAI_API_KEY=sk-... python examples/sdk_wrapper.py --live

A support agent may look up orders and refund small amounts, but only on the
caller's own orders. Everything else is denied before your code runs it.
"""
from __future__ import annotations

import json
import sys

import httpx
from openai import OpenAI

from x0r_core.guard import Caller, DecisionRecord, HookContext, Policy, ToolCallDenied, wrap

TOOLS = [
    {"type": "function", "function": {
        "name": "lookup_order",
        "description": "Look up an order by id.",
        "parameters": {"type": "object",
                       "properties": {"order_id": {"type": "string", "maxLength": 32}},
                       "required": ["order_id"]},
    }},
    {"type": "function", "function": {
        "name": "refund_order",
        "description": "Refund part or all of an order.",
        "parameters": {"type": "object",
                       "properties": {"order_id": {"type": "string", "maxLength": 32},
                                      "amount": {"type": "number", "minimum": 0}},
                       "required": ["order_id", "amount"]},
    }},
]


def own_orders_only(arguments: dict, ctx: HookContext) -> str | None:
    """Business rule: the caller may only touch their own orders."""
    if ctx.caller is None:
        return "caller_required"
    if arguments["order_id"] not in ctx.caller.attributes.get("orders", "").split(","):
        return "not_callers_record"
    return None


def small_refunds_only(arguments: dict, ctx: HookContext) -> str | None:
    reason = own_orders_only(arguments, ctx)
    if reason:
        return reason
    return "refund_over_limit" if arguments["amount"] > ctx.config["max_refund"] else None


def save_decision(record: DecisionRecord) -> None:
    # In a real app, write this to your database or log pipeline. It never holds arguments.
    print("decision:", json.dumps(record.__dict__))


POLICY = Policy(
    allow=["lookup_order", "refund_order"],
    rules={"lookup_order": own_orders_only, "refund_order": small_refunds_only},
    config={"max_refund": 50},
    on_decision=save_decision,
)


def fake_model() -> httpx.Client:
    """Stands in for the model: it asks to refund someone else's order."""
    reply = {
        "id": "chatcmpl-demo", "object": "chat.completion", "created": 0, "model": "demo",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "refund_order", "arguments": '{"order_id": "B-77", "amount": 900}'}}],
        }}],
    }
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply)))


def main() -> None:
    live = "--live" in sys.argv
    inner = OpenAI() if live else OpenAI(api_key="offline", base_url="http://demo.local/v1",
                                         http_client=fake_model())
    client = wrap(inner, POLICY)
    caller = Caller("user-42", {"orders": "A-1,A-2"})
    try:
        reply = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Refund order B-77 in full."}],
            tools=TOOLS,
            x0r_caller=caller,
        )
    except ToolCallDenied as denied:
        print("blocked:", denied)
        return
    for call in reply.choices[0].message.tool_calls or []:
        print("allowed, run it now:", call.function.name, call.function.arguments)
    if reply.choices[0].message.content:
        print("answer:", reply.choices[0].message.content)


if __name__ == "__main__":
    main()
