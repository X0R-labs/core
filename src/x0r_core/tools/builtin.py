from __future__ import annotations

import ast
import ipaddress
import math
import operator
import re
import socket

import httpx

from ..services.retrieval import search
from .base import Tool, ToolContext

# ── knowledge_search ─────────────────────────────────────────────────────────

async def knowledge_search(ctx: ToolContext, query: str, k: int = 4) -> dict:
    k = max(1, min(int(k), 10))
    hits = await search(ctx.session, ctx.workspace_id, query, k=k)
    return {
        "query": query,
        "results": [h.as_dict() for h in hits],
        "count": len(hits),
    }


KNOWLEDGE_SEARCH = Tool(
    name="knowledge_search",
    description=(
        "Search this workspace's private knowledge base. Use it whenever the answer "
        "might depend on the customer's own documents, policies or data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for."},
            "k": {"type": "integer", "description": "Number of passages (1-10).", "default": 4},
        },
        "required": ["query"],
    },
    handler=knowledge_search,
)

# ── calculator ───────────────────────────────────────────────────────────────

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
            raise ValueError("exponent too large")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = _FUNCS.get(node.func.id)
        if fn is None:
            raise ValueError(f"function not allowed: {node.func.id}")
        return fn(*(_eval(a) for a in node.args))
    raise ValueError("expression not allowed")


async def calculator(ctx: ToolContext, expression: str) -> dict:
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval(tree)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "expression": expression}
    return {"expression": expression, "result": value}


CALCULATOR = Tool(
    name="calculator",
    description="Evaluate a basic arithmetic expression safely (numbers, + - * / // % **, sqrt).",
    parameters={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "e.g. (1250 * 1.2) / 3"}
        },
        "required": ["expression"],
    },
    handler=calculator,
)

# ── http_get (allowlisted, SSRF-guarded) ─────────────────────────────────────

_TAG = re.compile(r"<[^>]+>")


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


async def http_get(ctx: ToolContext, url: str) -> dict:
    allowed = ctx.settings.allowed_http_hosts
    if not allowed:
        return {"error": "http_get is disabled. Set X0R_HTTP_TOOL_ALLOWLIST to enable it."}

    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"error": "only http/https URLs are allowed"}
    host = (parsed.hostname or "").lower()
    if host not in allowed:
        return {"error": f"host {host!r} is not in the allowlist"}
    if not _is_public_host(host):
        return {"error": "refusing to resolve to a private address"}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            resp = await client.get(url, headers={"User-Agent": "X0R-Core/0.1"})
        text = _TAG.sub(" ", resp.text)
        text = re.sub(r"\s+", " ", text).strip()
        return {"status": resp.status_code, "url": url, "text": text[:4000]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


HTTP_GET = Tool(
    name="http_get",
    description=(
        "Fetch a public web page from an allowlisted host and return its text. "
        "Disabled unless the operator configures an allowlist."
    ),
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL."}},
        "required": ["url"],
    },
    handler=http_get,
)

REGISTRY: dict[str, Tool] = {
    t.name: t for t in (KNOWLEDGE_SEARCH, CALCULATOR, HTTP_GET)
}


def resolve(names: list[str]) -> list[Tool]:
    return [REGISTRY[n] for n in names if n in REGISTRY]


def schemas(names: list[str]) -> list[dict]:
    return [tool.schema() for tool in resolve(names)]
