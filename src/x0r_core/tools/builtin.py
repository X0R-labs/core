from __future__ import annotations

import ast
import asyncio
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


def _number(value: int | float) -> int | float:
    if type(value) not in (int, float) or abs(value) > 10**100:
        raise ValueError("numeric limit exceeded")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("result must be finite")
    return value


def _eval(node: ast.AST, depth: int = 0) -> float:
    if depth > 32:
        raise ValueError("expression is too deeply nested")
    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return _number(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
            raise ValueError("exponent too large")
        return _number(_BIN_OPS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _number(_UNARY_OPS[type(node.op)](_eval(node.operand, depth + 1)))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = _FUNCS.get(node.func.id)
        if fn is None:
            raise ValueError(f"function not allowed: {node.func.id}")
        if node.keywords:
            raise ValueError("keyword arguments are not allowed")
        return _number(fn(*(_eval(a, depth + 1) for a in node.args)))
    raise ValueError("expression not allowed")


async def calculator(ctx: ToolContext, expression: str) -> dict:
    try:
        if len(expression) > 4096:
            raise ValueError("expression is too long")
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 256:
            raise ValueError("expression is too complex")
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
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            return False
    return bool(infos)


async def http_get(ctx: ToolContext, url: str) -> dict:
    allowed = ctx.settings.allowed_http_hosts
    if not allowed:
        return {"error": "http_get is disabled. Set X0R_HTTP_TOOL_ALLOWLIST to enable it."}

    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            return {"error": "invalid HTTP destination"}
    except ValueError:
        return {"error": "invalid HTTP destination"}
    if host not in allowed:
        return {"error": "host is not in the allowlist"}

    async def fetch() -> dict:
        if not await asyncio.to_thread(_is_public_host, host):
            return {"error": "refusing a non-public address"}
        async with httpx.AsyncClient(
            timeout=ctx.settings.http_tool_timeout, follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream(
                "GET", url,
                headers={"User-Agent": "X0R-Core/0.1", "Accept-Encoding": "identity"},
            ) as response:
                encoding = response.headers.get("content-encoding", "identity").lower().strip()
                if encoding not in {"", "identity"}:
                    return {"error": "compressed HTTP responses are not accepted"}
                if 300 <= response.status_code < 400:
                    return {"error": "HTTP redirects are not followed"}
                limit = ctx.settings.max_http_response_bytes
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        if int(length) < 0:
                            return {"error": "invalid HTTP content length"}
                        if int(length) > limit:
                            return {"error": "HTTP response exceeds byte limit"}
                    except ValueError:
                        return {"error": "invalid HTTP content length"}
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > limit:
                        return {"error": "HTTP response exceeds byte limit"}
                    data.extend(chunk)
                text = data.decode("utf-8", errors="replace")
                text = re.sub(r"\s+", " ", _TAG.sub(" ", text)).strip()
                return {"status": response.status_code, "url": url, "text": text[:4000]}

    try:
        async with asyncio.timeout(ctx.settings.http_tool_timeout):
            return await fetch()
    except TimeoutError:
        return {"error": "HTTP request exceeded time limit"}
    except httpx.HTTPError:
        return {"error": "HTTP request failed"}



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
