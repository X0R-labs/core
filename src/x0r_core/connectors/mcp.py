"""Policy enforcement for stdio and Streamable HTTP MCP upstreams."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import stat
import sys
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from threading import Thread
from time import monotonic
from uuid import uuid4

import httpx
from x0r_guard.engine import (
    POLICY_VERSION,
    ActionRequest,
    Authorizer,
    Caller,
    Decision,
    check_action,
)
from x0r_guard.engine import (
    schema_supported as schema_checkable,
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
SINK_TIMEOUT = 10
SHUTDOWN_TIMEOUT = 2
REPLAY_TTL = 600
REPLAY_LIMIT = 1000
REPLAY_MAX_BYTES = 32 * 1024 * 1024
REPLAY_MAX_BODY_BYTES = 1024 * 1024
SSE_LINE_END = re.compile(rb"[\r\n]")
RESOURCE_METHODS = {"resources/list", "resources/read", "resources/templates/list"}
PROMPT_METHODS = {"prompts/list", "prompts/get"}
PASSTHROUGH = {"initialize", "ping", "tools/list"} | RESOURCE_METHODS | PROMPT_METHODS
CLIENT_NOTIFICATIONS = {
    "notifications/initialized", "notifications/cancelled", "notifications/progress",
    "notifications/roots/list_changed",
}
SERVER_REQUESTS = {"ping", "sampling/createMessage", "roots/list", "elicitation/create"}


@dataclass(frozen=True)
class Tool:
    name: str
    parameters: dict
    authorize: Authorizer | None


@dataclass(frozen=True)
class ToolListing:
    generation: int
    first_page: bool


@dataclass
class Replay:
    fingerprint: bytes
    caller: str | None = None
    completed_at: float = 0
    body: bytes | None = None
    denial: str = "idempotency_in_progress"


def normalize_numbers(value: object) -> object:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [normalize_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_numbers(item) for key, item in value.items()}
    return value


def call_key(message: dict) -> tuple[str, bytes] | None:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or "idempotencyKey" not in meta:
        return None
    key = meta["idempotencyKey"]
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise ValueError("invalid_idempotency_key")
    canonical = json.dumps(
        [params.get("name"), normalize_numbers(params.get("arguments", {}))],
        sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    return key, hashlib.sha256(canonical).digest()


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


async def save_decision(sink: Callable[[DecisionRecord], None], record: DecisionRecord) -> None:
    result: Future[None] = Future()

    def save() -> None:
        if not result.set_running_or_notify_cancel():
            return
        try:
            sink(record)
        except BaseException as exc:
            result.set_exception(exc)
        else:
            result.set_result(None)

    # A blocked sink must not hold up event loop shutdown.
    Thread(target=save, daemon=True).start()
    await asyncio.wait_for(asyncio.wrap_future(result), SINK_TIMEOUT)


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
        limit=LINE_LIMIT, start_new_session=True,
    )
    try:
        yield child
    finally:
        child.stdin.close()
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        # Drain remaining pipe data so asyncio can reap a child that was blocked writing.
        drain = asyncio.create_task(child.communicate())
        try:
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(drain), SHUTDOWN_TIMEOUT)
        finally:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(drain, SHUTDOWN_TIMEOUT)


@dataclass(frozen=True)
class HttpUpstream:
    url: str
    token: str | None

    def __post_init__(self) -> None:
        url = httpx.URL(self.url)
        if not url.host or url.scheme not in {"https", "http"}:
            raise ValueError("--upstream requires an HTTP or HTTPS URL")
        if url.username or url.password:
            raise ValueError("use --upstream-token-env for upstream credentials")
        if url.scheme == "http" and url.host != "localhost":
            try:
                loopback = ip_address(url.host).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise ValueError("--upstream requires HTTPS except on localhost")

    @asynccontextmanager
    async def connect(self):
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        timeout = httpx.Timeout(30, read=None)
        async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=timeout) as client:
            yield HttpConnection(client, self.url)


class UpstreamRequestError(ValueError):
    pass


class HttpConnection:
    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self.client = client
        self.url = url
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    async def post(self, message: dict):
        headers = {}
        session_id = self.session_id
        if session_id is not None:
            headers["Mcp-Session-Id"] = session_id
        if self.protocol_version is not None:
            headers["MCP-Protocol-Version"] = self.protocol_version
        method = message.get("method")
        expects_reply = method is not None and "id" in message
        matched = False
        try:
            async with self.client.stream(
                "POST", self.url, headers=headers, content=encode(message),
            ) as response:
                if response.status_code == 404 and session_id is not None:
                    if self.session_id == session_id:
                        self.session_id = None
                    raise UpstreamRequestError("session expired; initialize again")
                response.raise_for_status()
                if not expects_reply and response.status_code == 202:
                    return
                if not expects_reply:
                    raise ValueError("expected HTTP 202 for a message without a reply")
                async for reply in http_messages(response):
                    if "method" in reply:
                        yield reply
                        continue
                    if matched or "id" not in reply or reply["id"] != message["id"]:
                        raise ValueError("unexpected JSON-RPC response id from HTTP upstream")
                    if method == "initialize" and isinstance(reply.get("result"), dict):
                        self.session_id = response.headers.get("Mcp-Session-Id")
                        version = reply["result"].get("protocolVersion")
                        if isinstance(version, str):
                            self.protocol_version = version
                    matched = True
                    yield reply
        except httpx.HTTPStatusError as exc:
            raise UpstreamRequestError(f"HTTP {exc.response.status_code}") from exc
        except httpx.TimeoutException as exc:
            raise UpstreamRequestError("timeout") from exc
        except httpx.HTTPError as exc:
            raise UpstreamRequestError("connection failed") from exc
        except UpstreamRequestError:
            raise
        except (ValueError, RecursionError) as exc:
            reason = str(exc) if type(exc) is ValueError else "invalid JSON-RPC response"
            raise UpstreamRequestError(reason) from exc
        if not matched:
            raise UpstreamRequestError("connection closed without a JSON-RPC response")


async def http_messages(response: httpx.Response):
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            body.extend(chunk)
            if len(body) > LINE_LIMIT:
                raise ValueError("HTTP upstream JSON response too large")
        yield decode(body)
    elif content_type == "text/event-stream":
        buffer = bytearray()
        fields = []
        size = 0
        first_line = True
        skip_lf = False
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            start = 0
            while start < len(buffer):
                if skip_lf:
                    if buffer[start] == 10:
                        start += 1
                    skip_lf = False
                match = SSE_LINE_END.search(buffer, start)
                if match is None:
                    break
                end = match.start()
                if end - start > LINE_LIMIT:
                    raise ValueError("HTTP upstream SSE line too large")
                line = bytes(buffer[start:end])
                start = match.end()
                skip_lf = match[0] == b"\r"
                if first_line:
                    line = line.removeprefix(b"\xef\xbb\xbf")
                    first_line = False
                if not line:
                    if any(fields):
                        yield decode(b"\n".join(fields))
                    fields.clear()
                    size = 0
                elif line == b"data" or line.startswith(b"data:"):
                    data = line[5:].removeprefix(b" ")
                    size += len(data) + 1
                    if size > LINE_LIMIT:
                        raise ValueError("HTTP upstream SSE event too large")
                    fields.append(data)
            del buffer[:start]
            if len(buffer) > LINE_LIMIT:
                raise ValueError("HTTP upstream SSE line too large")
    else:
        raise ValueError("unexpected HTTP upstream content type")


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
        self.tools_generation = 0
        self.pending: dict[str | int | None, ToolListing | None] = {}
        self.replays: OrderedDict[str, Replay] = OrderedDict()
        self.replay_bytes = 0
        self.in_flight: dict[str, Replay] = {}
        self.call_keys: dict[str | int | None, str] = {}

    async def record_decision(self, record: DecisionRecord) -> bool:
        saved = True
        for sink in self.sinks:
            try:
                await save_decision(sink, record)
            except Exception:  # A sink failure must stop execution.
                print("mcp-proxy: could not save decision", file=sys.stderr)
                saved = False
        return saved

    async def decide(
        self, message: dict, keyed: tuple[str, Replay] | None = None,
        *, invalid_key: bool = False,
    ) -> Decision:
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        name = params.get("name")
        name = name if isinstance(name, str) and name else None
        tool = self.tools.get(name) if name is not None else None
        call_id = str(message["id"]) if "id" in message else None
        caller = None
        try:
            if self.get_caller is not None:
                caller = self.get_caller()
        except CallerTokenError as exc:
            decision = Decision(False, exc.reason)
        else:
            if invalid_key:
                decision = Decision(False, "invalid_idempotency_key")
            elif name is None or "id" not in message:
                decision = Decision(False, "invalid_request")
            elif message["id"] in self.pending:
                decision = Decision(False, "duplicate_request_id")
            else:
                if name in self.tools and tool is None:
                    decision = Decision(False, "invalid_arguments")
                else:
                    decision = check_action(
                        ActionRequest("mcp", "mcp", {name}, name, params.get("arguments", {}),
                                      caller=caller, tool_call_id=call_id),
                        operator_tools=self.policy.allow,
                        tools={name: tool} if tool is not None else {},
                    )
                if keyed is not None:
                    key, attempt = keyed
                    attempt.caller = caller.subject if caller else None
                    existing = self.in_flight.get(key) or self.replays.get(key)
                    if existing is not None:
                        if (existing.fingerprint != attempt.fingerprint
                                or existing.caller != attempt.caller):
                            decision = Decision(False, "idempotency_key_reused")
                        elif existing.body is None:
                            if decision.allowed or key in self.replays:
                                decision = Decision(False, existing.denial)
                        elif decision.allowed:
                            attempt.body = existing.body
                            decision = Decision(True, "idempotency_replayed")
        record = DecisionRecord(
            tool=name or "(missing tool name)", tool_call_id=call_id,
            allowed=decision.allowed, reason=decision.reason,
            caller_subject=caller.subject if caller else None, policy_version=POLICY_VERSION,
            decided_at=datetime.now(UTC).isoformat(), policy_digest=self.policy.digest,
        )
        if not await self.record_decision(record):
            return Decision(False, "decision_not_recorded")
        if decision.allowed and self.tools.get(name) is not tool:
            decision = Decision(False, "tools_changed")
            if not await self.record_decision(replace(
                record, allowed=False, reason=decision.reason,
                decision_id=str(uuid4()),
                decided_at=datetime.now(UTC).isoformat(),
            )):
                return Decision(False, "decision_not_recorded")
        return decision

    def drop_oldest_replay(self) -> None:
        _, attempt = self.replays.popitem(last=False)
        if attempt.body is not None:
            self.replay_bytes -= len(attempt.body)

    def expire_replays(self) -> None:
        now = monotonic()
        while self.replays:
            oldest = next(iter(self.replays.values()))
            if now - oldest.completed_at < REPLAY_TTL:
                break
            self.drop_oldest_replay()

    def finish_call(self, request_id: str | int | None, response: dict | None = None) -> None:
        key = self.call_keys.pop(request_id, None)
        if key is None:
            return
        attempt = self.in_flight.pop(key)
        attempt.denial = "idempotency_outcome_unknown"
        # JSON-RPC errors are upstream outcomes too. Retrying may repeat a side effect.
        if response is not None and (("result" in response) != ("error" in response)):
            field = "result" if "result" in response else "error"
            body = encode({field: response[field]}).rstrip(b"\n")
            if len(body) <= REPLAY_MAX_BODY_BYTES:
                attempt.body = body
                self.replay_bytes += len(body)
            else:
                attempt.denial = "idempotency_result_too_large"
        attempt.completed_at = monotonic()
        self.replays[key] = attempt
        while len(self.replays) > REPLAY_LIMIT or self.replay_bytes > REPLAY_MAX_BYTES:
            self.drop_oldest_replay()

    async def accept_client(self, message: dict) -> bool:
        self.expire_replays()
        method = message.get("method")
        if method is None:
            return True
        if "id" not in message:
            if method in CLIENT_NOTIFICATIONS:
                return True
            print(f"mcp-proxy: dropped client notification {method!r}", file=sys.stderr)
            return False
        if method == "tools/call":
            try:
                identity = call_key(message)
            except ValueError:
                keyed = None
                decision = await self.decide(message, invalid_key=True)
            else:
                keyed = (identity[0], Replay(identity[1])) if identity is not None else None
                decision = await self.decide(message, keyed)
            if not decision.allowed:
                reply(message, result={
                    "content": [{"type": "text", "text": f"Denied by policy: {decision.reason}"}],
                    "isError": True,
                })
                return False
            if keyed is not None:
                key, attempt = keyed
                if attempt.body is not None:
                    reply(message, **json.loads(attempt.body))
                    return False
                self.in_flight[key] = attempt
                self.call_keys[message["id"]] = key
        else:
            if (
                method not in PASSTHROUGH
                or (self.block_resources and method in RESOURCE_METHODS)
                or (self.block_prompts and method in PROMPT_METHODS)
            ):
                reply(message, error={"code": -32601, "message": "Method not available"})
                return False
            if message["id"] in self.pending:
                reply(message, error={"code": -32600, "message": "Request id already in use"})
                return False
        listing = None
        if method == "tools/list":
            params = message.get("params")
            first_page = not isinstance(params, dict) or params.get("cursor") is None
            listing = ToolListing(self.tools_generation, first_page)
        self.pending[message["id"]] = listing
        return True

    def accept_server(self, message: dict) -> dict | None:
        self.expire_replays()
        if "method" in message:
            method = message["method"]
            if "id" in message:
                if method not in SERVER_REQUESTS:
                    return {
                        "jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32601, "message": "Method not available"},
                    }
            elif method == "notifications/tools/list_changed":
                self.tools_generation += 1
                self.tools.clear()
        elif "id" in message:
            request_id = message["id"]
            listing = self.pending.pop(request_id, None)
            self.finish_call(request_id, message)
            if listing is not None and "result" in message:
                result = message["result"]
                try:
                    entries = tool_entries(result)
                except ValueError as exc:
                    raise UpstreamRequestError(str(exc)) from exc
                current = listing.generation == self.tools_generation
                if current and listing.first_page:
                    self.tools.clear()
                visible = []
                for entry in entries:
                    name = entry["name"]
                    if name in self.policy.allow:
                        visible.append(entry)
                        if current:
                            schema = entry.get("inputSchema")
                            self.tools[name] = (
                                Tool(name, schema, self.policy.rules.get(name))
                                if schema_checkable(schema) else None
                            )
                result["tools"] = visible
        write_client(message)
        return None

    async def run_http(self, upstream: HttpUpstream) -> int:
        async with upstream.connect() as connection, client_reader() as readline:
            async def exchange(message: dict, initialized: asyncio.Event | None = None):
                awaiting_reply = "id" in message and "method" in message
                try:
                    async with aclosing(connection.post(message)) as replies:
                        async for response in replies:
                            error = self.accept_server(response)
                            if "method" not in response:
                                awaiting_reply = False
                                if initialized is not None:
                                    initialized.set()
                            if error is not None:
                                await exchange(error)
                except UpstreamRequestError as exc:
                    self.expire_replays()
                    if awaiting_reply:
                        request_id = message["id"]
                        self.pending.pop(request_id, None)
                        self.finish_call(request_id)
                        reply(message, error={
                            "code": -32000, "message": f"Upstream request failed: {exc}",
                        })
                finally:
                    if initialized is not None:
                        initialized.set()

            reading = asyncio.create_task(readline())
            active = {reading}
            try:
                while active:
                    done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    active.difference_update(done)
                    # Apply upstream changes before deciding the next client call.
                    for task in done - {reading}:
                        task.result()
                    if reading in done and (line := reading.result()):
                        message = decode(line)
                        if await self.accept_client(message):
                            if message.get("method") == "initialize":
                                initialized = asyncio.Event()
                                active.add(asyncio.create_task(exchange(message, initialized)))
                                await initialized.wait()
                            elif "id" not in message:
                                await exchange(message)
                            else:
                                active.add(asyncio.create_task(exchange(message)))
                        reading = asyncio.create_task(readline())
                        active.add(reading)
                return 0
            finally:
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)

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
                    error = self.accept_server(decode(line))
                    if error is not None:
                        child.stdin.write(encode(error))
                        await child.stdin.drain()

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
                for request_id in list(self.call_keys):
                    self.pending.pop(request_id, None)
                    self.finish_call(request_id)


@asynccontextmanager
async def check_stdio(command: list[str]):
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

        async def notify(message: dict):
            child.stdin.write(encode(message))
            await child.stdin.drain()

        yield request, notify


@asynccontextmanager
async def check_http(upstream: HttpUpstream):
    async with upstream.connect() as connection:
        async def notify(message: dict):
            async with aclosing(connection.post(message)) as replies:
                async for _ in replies:
                    pass

        async def request(request_id: int, method: str, params: dict) -> dict:
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            async with aclosing(connection.post(message)) as replies:
                async for response in replies:
                    if "method" in response:
                        if "id" in response:
                            await notify({
                                "jsonrpc": "2.0", "id": response["id"],
                                "error": {"code": -32601, "message": "Method not available"},
                            })
                    elif isinstance(response.get("result"), dict):
                        return response["result"]
                    else:
                        break
            raise ValueError(f"upstream rejected {method}")

        yield request, notify


async def check_server(command: list[str] | HttpUpstream, policy: PolicyFile) -> int:
    transport = check_http(command) if isinstance(command, HttpUpstream) else check_stdio(command)
    async with transport as (request, notify):
        await request(1, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "x0r mcp-proxy", "version": "0.1.0"},
        })
        await notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
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
