"""Policy enforcement for newline-delimited MCP over stdio."""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import stat
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from x0r_guard.engine import (
    POLICY_VERSION,
    PROPERTY_KEYWORDS,
    ROOT_KEYWORDS,
    ActionRequest,
    Authorizer,
    Caller,
    Decision,
    check_action,
)
from x0r_guard.identity import (
    MAX_TOKEN_LENGTH,
    MIN_SECRET_BYTES,
    CallerTokenError,
    token_key_id,
    verify_caller,
)
from x0r_guard.policy_file import PolicyFile
from x0r_guard.sdk import DecisionRecord

LINE_LIMIT = 8 * 1024 * 1024
RESOURCE_METHODS = {"resources/list", "resources/read", "resources/templates/list"}
PROMPT_METHODS = {"prompts/list", "prompts/get"}
PASSTHROUGH = {"initialize", "ping", "tools/list"} | RESOURCE_METHODS | PROMPT_METHODS


@dataclass(frozen=True)
class Tool:
    name: str
    parameters: dict
    authorize: Authorizer | None


def schema_checkable(schema: object) -> bool:
    # The engine checks supplied values. Check every property here, including optional ones.
    if (not isinstance(schema, dict) or schema.keys() - ROOT_KEYWORDS
            or schema.get("type", "object") != "object"
            or type(schema.get("additionalProperties", False)) is not bool):
        return False
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        return False
    if any(not isinstance(name, str) or name not in properties for name in required):
        return False
    for spec in properties.values():
        if not isinstance(spec, dict) or spec.keys() - PROPERTY_KEYWORDS:
            return False
        kind = spec.get("type")
        if kind not in ("string", "integer", "number", "boolean"):
            return False
        if "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
            return False
        for keyword in spec.keys() & {"minLength", "maxLength"}:
            if kind != "string" or type(spec[keyword]) is not int or spec[keyword] < 0:
                return False
        for keyword in spec.keys() & {"minimum", "maximum"}:
            value = spec[keyword]
            if kind not in ("integer", "number"):
                return False
            if type(value) is not int and (type(value) is not float or not math.isfinite(value)):
                return False
    return True


def caller_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if len(key) >= MIN_SECRET_BYTES:
            return key
    except ValueError:
        pass
    raise ValueError("X0R_CALLER_KEY must contain a base64url audience verification key")


def read_caller(path: Path, key: bytes, audience: str) -> Caller:
    try:
        with path.open(encoding="utf-8") as file:
            token = file.read(MAX_TOKEN_LENGTH + 2)
    except (OSError, UnicodeError) as exc:
        raise CallerTokenError("caller_token_unreadable", "cannot read caller token") from exc
    if len(token) == MAX_TOKEN_LENGTH + 2:
        raise CallerTokenError("caller_token_malformed", "caller token file is too long")
    token = token.strip()
    return verify_caller(token, keys={token_key_id(token): key}, audience=audience)


def append_decision(path: Path, record: DecisionRecord) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(asdict(record)) + "\n")


def decode(line: bytes) -> dict:
    message = json.loads(line)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise ValueError("expected a JSON-RPC 2.0 object")
    if "id" in message and type(message["id"]) not in (str, int, type(None)):
        raise ValueError("invalid JSON-RPC id")
    if "method" in message and not isinstance(message["method"], str):
        raise ValueError("invalid JSON-RPC method")
    return message


def encode(message: dict) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("ascii")


def write_client(message: dict) -> None:
    sys.stdout.buffer.write(encode(message))
    sys.stdout.buffer.flush()


def reply(message: dict, **body: object) -> None:
    if "id" in message:
        write_client({"jsonrpc": "2.0", "id": message["id"], **body})


def tool_entries(result: object) -> list[dict]:
    entries = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        raise ValueError("expected a tools/list result with a tools array")
    return [
        entry for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]


@asynccontextmanager
async def upstream(command: list[str]):
    child = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        env={name: value for name, value in os.environ.items() if not name.startswith("X0R_")},
        limit=LINE_LIMIT,
    )
    try:
        yield child
    finally:
        child.stdin.close()
        if child.returncode is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
        # Drain remaining pipe data so asyncio can reap a child that was blocked writing.
        try:
            await asyncio.wait_for(child.communicate(), 2)
        except TimeoutError:
            if child.returncode is None:
                child.kill()
            await child.communicate()


@asynccontextmanager
async def client_reader():
    if not stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=LINE_LIMIT)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
        )
        try:
            yield reader.readline
        finally:
            transport.close()
    else:
        async def readline():
            return sys.stdin.buffer.readline(LINE_LIMIT + 1)

        yield readline


class Proxy:
    def __init__(
        self,
        policy: PolicyFile,
        sinks: list[Callable[[DecisionRecord], None]],
        *,
        get_caller: Callable[[], Caller] | None,
        block_resources: bool,
        block_prompts: bool,
    ) -> None:
        self.policy = policy
        self.sinks = sinks
        self.get_caller = get_caller
        self.block_resources = block_resources
        self.block_prompts = block_prompts
        self.tools: dict[str, Tool | None] = {}
        self.pending: dict[str | int | None, str] = {}

    async def decide(self, message: dict) -> Decision:
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        name = params.get("name")
        name = name if isinstance(name, str) and name else None
        call_id = str(message["id"]) if "id" in message else None
        caller = None
        try:
            if self.get_caller is not None:
                caller = self.get_caller()
        except CallerTokenError as exc:
            decision = Decision(False, exc.reason)
        else:
            if name is None or "id" not in message:
                decision = Decision(False, "invalid_request")
            elif message["id"] in self.pending:
                decision = Decision(False, "duplicate_request_id")
            elif name in self.tools and self.tools[name] is None:
                decision = Decision(False, "invalid_arguments")
            else:
                tool = self.tools.get(name)
                decision = check_action(
                    ActionRequest("mcp", "mcp", {name}, name, params.get("arguments", {}),
                                  caller=caller, tool_call_id=call_id),
                    operator_tools=self.policy.allow,
                    tools={name: tool} if tool is not None else {},
                )
        record = DecisionRecord(
            tool=name or "(missing tool name)", tool_call_id=call_id,
            allowed=decision.allowed, reason=decision.reason,
            caller_subject=caller.subject if caller else None, policy_version=POLICY_VERSION,
            decided_at=datetime.now(UTC).isoformat(), policy_digest=self.policy.digest,
        )
        failed = False
        for sink in self.sinks:
            try:
                await asyncio.to_thread(sink, record)
            except Exception:  # A sink failure must stop execution.
                print("mcp-proxy: could not save decision", file=sys.stderr)
                failed = True
        return Decision(False, "decision_not_recorded") if failed else decision

    async def accept_client(self, message: dict) -> bool:
        method = message.get("method")
        if method is None:
            return True
        if method == "tools/call":
            decision = await self.decide(message)
            if not decision.allowed:
                reply(message, result={
                    "content": [{"type": "text", "text": f"Denied by policy: {decision.reason}"}],
                    "isError": True,
                })
                return False
        else:
            notification = "id" not in message and method.startswith("notifications/")
            if (
                (method not in PASSTHROUGH and not notification)
                or (self.block_resources and method in RESOURCE_METHODS)
                or (self.block_prompts and method in PROMPT_METHODS)
            ):
                reply(message, error={"code": -32601, "message": "Method not available"})
                return False
            if "id" in message and message["id"] in self.pending:
                reply(message, error={"code": -32600, "message": "Request id already in use"})
                return False
        if "id" in message:
            self.pending[message["id"]] = method
        return True

    def accept_server(self, message: dict) -> None:
        if "method" in message:
            if message["method"] == "notifications/tools/list_changed":
                self.tools.clear()
        elif "id" in message:
            method = self.pending.pop(message["id"], None)
            if method == "tools/list" and "result" in message:
                result = message["result"]
                visible = []
                for entry in tool_entries(result):
                    name = entry["name"]
                    if name in self.policy.allow:
                        schema = entry.get("inputSchema")
                        self.tools[name] = (
                            Tool(name, schema, self.policy.rules.get(name))
                            if schema_checkable(schema) else None
                        )
                        visible.append(entry)
                result["tools"] = visible
        write_client(message)

    async def run(self, command: list[str]) -> int:
        async with upstream(command) as child, client_reader() as readline:
            async def send_requests():
                while line := await readline():
                    message = decode(line)
                    if await self.accept_client(message):
                        child.stdin.write(encode(message))
                        await child.stdin.drain()
                child.stdin.close()

            async def send_responses():
                while line := await child.stdout.readline():
                    self.accept_server(decode(line))

            requests = asyncio.create_task(send_requests())
            responses = asyncio.create_task(send_responses())
            try:
                done, _ = await asyncio.wait(
                    (requests, responses), return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task.result()
                if not responses.done():
                    await asyncio.wait_for(responses, 5)
                return await asyncio.wait_for(child.wait(), 5)
            finally:
                for task in (requests, responses):
                    task.cancel()
                await asyncio.gather(requests, responses, return_exceptions=True)


async def check_server(command: list[str], policy: PolicyFile) -> int:
    async with upstream(command) as child:
        async def request(request_id: int, method: str, params: dict) -> dict:
            child.stdin.write(encode({
                "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
            }))
            await child.stdin.drain()
            async with asyncio.timeout(10):
                while line := await child.stdout.readline():
                    message = decode(line)
                    if "method" in message and "id" in message:
                        child.stdin.write(encode({
                            "jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "Method not available"},
                        }))
                        await child.stdin.drain()
                    elif "method" not in message and message.get("id") == request_id:
                        if isinstance(message.get("result"), dict):
                            return message["result"]
                        raise ValueError(f"upstream rejected {method}")
            raise ValueError(f"upstream closed stdout during {method}")

        await request(1, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "x0r mcp-proxy", "version": "0.1.0"},
        })
        child.stdin.write(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        failed = False
        params: dict = {}
        cursors: set[str] = set()
        request_id = 2
        while True:
            result = await request(request_id, "tools/list", params)
            for entry in tool_entries(result):
                name = entry["name"]
                allowed = name in policy.allow
                checkable = schema_checkable(entry.get("inputSchema"))
                grant = "allowed" if allowed else "not-in-policy"
                schema = "checkable" if checkable else "uncheckable"
                print(f"{name}: {grant}, schema {schema}")
                failed |= allowed and not checkable
            cursor = result.get("nextCursor")
            if cursor is None:
                return int(failed)
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("invalid tools/list cursor")
            cursors.add(cursor)
            params = {"cursor": cursor}
            request_id += 1
