"""Guard your own OpenAI app in one line: no Core server needed.

    pip install -e . openai
    python examples/sdk_wrapper.py            # offline: a fake model asks for a refund
    python examples/sdk_wrapper.py --play     # type requests yourself and watch the guard
    OPENAI_API_KEY=sk-... python examples/sdk_wrapper.py --play --live

A support agent may look up orders and refund up to 50, but only on the caller's
own orders (A-1 and A-2). Everything else is denied before your code runs it.
Offline, a tiny fake model turns your words into a tool call: it looks for an
order id like A-1 and an amount like 20.
"""
from __future__ import annotations

import json
import re
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


def fake_call(text: str) -> tuple[str, dict]:
    """A stand-in for the model: pick a tool and arguments from plain words."""
    order = re.search(r"\b([A-Za-z]-\d+)\b", text)
    amount = re.search(r"(?<![\w-])\$?(\d+(?:\.\d+)?)\b", text)
    args: dict = {"order_id": order.group(1).upper() if order else "A-1"}
    if "refund" in text.lower():
        args["amount"] = float(amount.group(1)) if amount else 10
        return "refund_order", args
    return "lookup_order", args


def fake_model() -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        name, args = fake_call(messages[-1]["content"])
        return httpx.Response(200, json={
            "id": "chatcmpl-demo", "object": "chat.completion", "created": 0, "model": "demo",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args)}}],
            }}],
        })
    return httpx.Client(transport=httpx.MockTransport(answer))


def ask(client, text: str) -> None:
    caller = Caller("user-42", {"orders": "A-1,A-2"})
    try:
        reply = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are a support agent. Use the tools."},
                      {"role": "user", "content": text}],
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


def main() -> None:
    live = "--live" in sys.argv
    inner = OpenAI() if live else OpenAI(api_key="offline", base_url="http://demo.local/v1",
                                         http_client=fake_model())
    client = wrap(inner, POLICY)
    if "--play" not in sys.argv:
        ask(client, "Refund order B-77 for 900.")
        return
    print("You are user-42 and own orders A-1 and A-2. Refunds over 50 are blocked.")
    print("Try: look up A-1 / refund A-1 for 20 / refund A-1 for 900 / refund B-77 for 5")
    print("Empty line to quit.\n")
    while text := input("> ").strip():
        ask(client, text)
        print()


if __name__ == "__main__":
    main()
