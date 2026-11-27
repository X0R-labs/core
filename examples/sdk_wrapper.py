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
    owned = ctx.caller.attributes.get("orders", ())
    if isinstance(owned, str):
        owned = tuple(owned.split(","))   # older apps send "A-1,A-2"
    if arguments["order_id"] not in owned:
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


_ORDER = re.compile(r"\b([A-Za-z]-\d+)\b")
_AMOUNT = re.compile(r"(?:\bfor\s+|\$)\s*(-?\$?[\d,]+(?:\.\d+)?)", re.IGNORECASE)
_NEGATION = re.compile(r"\b(don'?t|do not|never|no|cancel|stop)\b", re.IGNORECASE)
HELP = "Try: look up A-1 / refund A-1 for 20 / refund A-1 for $1,000 / refund B-77 for 5"


def fake_call(text: str) -> tuple[str, dict] | str:
    """A stand-in for the model: pick a tool and arguments from plain words.

    Returns (tool, arguments), or a plain answer when the request isn't clear. It never
    fills in an order or amount you didn't give, so what the guard checks is what you typed.
    """
    lower = text.lower()
    orders = _ORDER.findall(text)
    if _NEGATION.search(text):
        return "Okay, I won't do anything."
    if len(orders) != 1:
        return "Which order? Name exactly one, like A-1. " + HELP
    order_id = orders[0].upper()
    if "refund" in lower:
        amounts = _AMOUNT.findall(text)
        if len(amounts) != 1:
            return "How much? Say it like 'refund A-1 for 20'."
        try:
            amount = float(amounts[0].replace("$", "").replace(",", ""))
        except ValueError:
            return "I couldn't read that amount."
        # A negative amount goes to the guard as asked: the schema denies it.
        return "refund_order", {"order_id": order_id, "amount": amount}
    if re.search(r"\b(look ?up|check|status|find|show)\b", lower):
        return "lookup_order", {"order_id": order_id}
    return "I can look up or refund an order. " + HELP


def fake_model() -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        result = fake_call(messages[-1]["content"])
        message: dict = {"role": "assistant", "content": None}
        if isinstance(result, str):
            message["content"] = result
        else:
            name, args = result
            message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}]
        return httpx.Response(200, json={
            "id": "chatcmpl-demo", "object": "chat.completion", "created": 0, "model": "demo",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "stop" if message["content"] else "tool_calls"}],
        })
    return httpx.Client(transport=httpx.MockTransport(answer))


def ask(client, text: str) -> None:
    caller = Caller("user-42", {"orders": ("A-1", "A-2")})
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
    print(HELP)
    print("Empty line to quit.\n")
    while text := input("> ").strip():
        ask(client, text)
        print()


if __name__ == "__main__":
    main()
