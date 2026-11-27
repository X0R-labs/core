"""Exercise the installed CLI and a real child process over stdio."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from typer.testing import CliRunner
from x0r_guard import sink
from x0r_guard.engine import Caller
from x0r_guard.identity import audience_key, sign_caller
from x0r_guard.policy_file import load_policy_file
from x0r_guard.sdk import DecisionRecord

from x0r_core.cli import app
from x0r_core.connectors import mcp

SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


@pytest.fixture
def setup_proxy(tmp_path):
    policy = tmp_path / "policy.toml"
    policy.write_text(
        'version = 1\nallow = ["refund"]\n'
        '[rules.refund]\nmax = { argument = "amount", value = 50 }\n',
        encoding="utf-8",
    )
    return policy, tmp_path / "calls.jsonl", tmp_path / "decisions.jsonl"


def command(policy, calls, *flags, server_args=()):
    return [
        sys.executable, "-m", "x0r_core.cli", "mcp-proxy", "--policy", str(policy),
        *flags, "--", sys.executable, str(SERVER), str(calls), *server_args,
    ]


class Client:
    def __init__(self, process):
        self.process = process
        self.next_id = 0

    def send(self, **message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def receive(self):
        line = self.process.stdout.readline()
        assert line
        return json.loads(line)

    def request(self, method, **params):
        self.next_id += 1
        self.send(method=method, id=self.next_id, params=params)
        reply = self.receive()
        assert reply["id"] == self.next_id
        return reply

    def call(self, name, **arguments):
        return self.request("tools/call", name=name, arguments=arguments)["result"]


@contextmanager
def client(policy, calls, *flags, env=None, server_args=()):
    with subprocess.Popen(
        command(policy, calls, *flags, server_args=server_args),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    ) as process:
        try:
            cli = Client(process)
            cli.request("initialize", protocolVersion="2025-03-26")
            yield cli
            process.stdin.close()
            assert process.wait(timeout=10) == 0, process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def denied(reason):
    return {"content": [{"type": "text", "text": f"Denied by policy: {reason}"}], "isError": True}


def test_guard_and_log(setup_proxy):
    policy, calls, log = setup_proxy
    env = {**os.environ, "X0R_API_KEY": "secret", "X0R_EXTRA": "secret"}
    with client(policy, calls, "--decision-log", str(log), env=env) as cli:
        assert cli.call("refund", amount=5) == denied("unknown_tool")
        visible = cli.request("tools/list")["result"]["tools"]
        assert [tool["name"] for tool in visible] == ["refund"]
        assert cli.call("hidden") == denied("not_granted_by_operator")
        assert cli.call("refund", amount=51) == denied("rule_max")
        assert cli.call("refund", amount="5") == denied("invalid_arguments")
        assert not calls.exists()
        result = cli.call("refund", amount=50)
        assert result["content"] == [{"type": "text", "text": "refunded"}]
        assert result["x0r_env"] == []
    assert rows(calls) == [{"name": "refund", "arguments": {"amount": 50}}]
    records = rows(log)
    assert [r["tool_call_id"] for r in records] == ["2", "4", "5", "6", "7"]
    assert [r["allowed"] for r in records] == [False, False, False, False, True]
    digest = hashlib.sha256(policy.read_bytes()).hexdigest()
    assert {r["policy_digest"] for r in records} == {digest}


def test_token_is_read_for_each_call(setup_proxy, tmp_path):
    policy, calls, log = setup_proxy
    token_file = tmp_path / "caller"
    root = b"k" * 32
    audience = "orders"
    key = base64.urlsafe_b64encode(audience_key(root, audience)).decode().rstrip("=")
    env = {**os.environ, "X0R_CALLER_KEY": key}

    def write_token(subject, now=None):
        token_file.write_text(sign_caller(
            Caller(subject), key_id="ck_1", secret=root, audience=audience, now=now,
        ), encoding="utf-8")

    write_token("alice")
    with client(
        policy, calls, "--decision-log", str(log), "--caller-token-file", str(token_file),
        "--audience", audience, env=env,
    ) as cli:
        cli.request("tools/list")
        result = cli.call("refund", amount=1)
        assert result["x0r_env"] == []
        assert result["argv"] == []
        token_file.write_text("bad", encoding="utf-8")
        assert cli.call("refund", amount=2) == denied("caller_token_malformed")
        write_token("bob", now=time.time() - 1000)
        assert cli.call("refund", amount=3) == denied("caller_expired")
        write_token("bob")
        assert cli.call("refund", amount=4)["content"][0]["text"] == "refunded"
    assert [row["caller_subject"] for row in rows(log)] == ["alice", None, None, "bob"]
    assert [row["arguments"]["amount"] for row in rows(calls)] == [1, 4]


def test_save_failure_denies(setup_proxy, tmp_path):
    policy, calls, _ = setup_proxy
    with client(policy, calls, "--decision-log", str(tmp_path)) as cli:
        cli.request("tools/list")
        assert cli.call("refund", amount=1) == denied("decision_not_recorded")
    assert not calls.exists()


def test_unsupported_optional_schema_denies_even_without_argument(setup_proxy):
    policy, calls, log = setup_proxy
    policy.write_text('version = 1\nallow = ["bulk"]\n', encoding="utf-8")
    with client(policy, calls, "--decision-log", str(log)) as cli:
        assert cli.request("tools/list")["result"]["tools"][0]["name"] == "bulk"
        assert cli.call("bulk") == denied("invalid_arguments")
    assert not calls.exists()


@pytest.mark.parametrize("block", [False, True])
def test_methods_and_server_requests(setup_proxy, block):
    policy, calls, log = setup_proxy
    flags = ("--block-resources", "--block-prompts") if block else ()
    with client(policy, calls, "--decision-log", str(log), *flags, server_args=("--roots",)) as cli:
        cli.send(method="notifications/initialized")
        assert cli.receive()["method"] == "roots/list"
        cli.send(id="roots", result={"roots": []})
        assert cli.receive()["method"] == "notifications/roots_received"
        assert cli.request("unknown/method")["error"]["code"] == -32601
        assert cli.request("ping")["result"] == {"method": "ping"}
        for method in ("resources/list", "resources/read", "resources/templates/list",
                       "prompts/list", "prompts/get"):
            response = cli.request(method)
            if block:
                assert response["error"]["code"] == -32601
            else:
                assert response["result"] == {"method": method}


def run_cli(policy, calls, *flags):
    return subprocess.run(
        command(policy, calls, *flags, server_args=("--paged",)), capture_output=True,
        text=True, timeout=15,
    )


@pytest.mark.parametrize("allow_bulk", [False, True])
def test_check(setup_proxy, allow_bulk):
    policy, calls, _ = setup_proxy
    if allow_bulk:
        policy.write_text('version = 1\nallow = ["refund", "bulk"]\n', encoding="utf-8")
    result = run_cli(policy, calls, "--check")
    assert result.returncode == int(allow_bulk), result.stderr
    assert result.stdout.splitlines() == [
        "refund: allowed, schema checkable",
        "hidden: not-in-policy, schema checkable",
        f"bulk: {'allowed' if allow_bulk else 'not-in-policy'}, schema uncheckable",
    ]
    assert not calls.exists()


def test_bad_policy_and_missing_sink(setup_proxy):
    policy, calls, _ = setup_proxy
    result = run_cli(policy, calls)
    assert result.returncode == 2
    assert "--decision-log" in result.stderr
    policy.write_text("bad TOML", encoding="utf-8")
    result = run_cli(policy, calls, "--check")
    assert result.returncode == 2
    assert "invalid TOML" in result.stderr
    assert str(policy) in result.stderr
    assert not result.stdout


@pytest.fixture
def proxy(setup_proxy, monkeypatch):
    policy, _, _ = setup_proxy
    instance = mcp.Proxy(load_policy_file(policy), [], get_caller=None,
                         block_resources=False, block_prompts=False)
    monkeypatch.setattr(mcp, "write_client", Mock())
    return instance


async def test_client_notification_allowlist(proxy, capsys):
    for method in ("notifications/initialized", "notifications/cancelled", "notifications/progress",
                   "notifications/roots/list_changed"):
        assert await proxy.accept_client({"method": method})
    for method in ("notifications/tools/list_changed", "ping", "tools/call"):
        assert not await proxy.accept_client({"method": method})
        assert method in capsys.readouterr().err
    assert not proxy.pending


def test_server_request_allowlist(proxy):
    for method in ("ping", "sampling/createMessage", "roots/list", "elicitation/create"):
        message = {"jsonrpc": "2.0", "method": method, "id": "server"}
        assert proxy.accept_server(message) is None
        mcp.write_client.assert_called_with(message)
    mcp.write_client.reset_mock()
    assert proxy.accept_server({"method": "tools/call", "id": "server"}) == {
        "jsonrpc": "2.0", "id": "server",
        "error": {"code": -32601, "message": "Method not available"},
    }
    mcp.write_client.assert_not_called()


def refund_entry():
    return {"name": "refund", "inputSchema": {
        "type": "object", "properties": {"amount": {"type": "integer"}},
    }}


async def list_tools(proxy, *entries, cursor=None):
    params = {} if cursor is None else {"cursor": cursor}
    assert await proxy.accept_client({"method": "tools/list", "id": 1, "params": params})
    proxy.accept_server({"id": 1, "result": {"tools": list(entries)}})


@pytest.mark.parametrize("clear", [True, False])
async def test_tool_changed_during_save(proxy, clear):
    await list_tools(proxy, refund_entry())
    loop = asyncio.get_running_loop()
    records = []

    def change_tool():
        if clear:
            proxy.accept_server({"method": "notifications/tools/list_changed"})
        else:
            proxy.tools["refund"] = replace(proxy.tools["refund"])

    def save(record):
        records.append(record)
        if record.allowed:
            loop.call_soon_threadsafe(change_tool)

    # Schedule the cache update on the event loop before the sink completes.
    proxy.sinks.append(save)
    message = {"id": 2, "method": "tools/call",
               "params": {"name": "refund", "arguments": {"amount": 1}}}
    assert not await proxy.accept_client(message)
    assert mcp.write_client.call_args.args[0]["result"] == denied("tools_changed")
    allowed, blocked = records
    assert allowed.allowed
    assert not blocked.allowed
    assert blocked.reason == "tools_changed"
    assert allowed.decision_id != blocked.decision_id


async def test_old_list_response_is_filtered_without_caching(proxy):
    assert await proxy.accept_client({"method": "tools/list", "id": 2})
    proxy.accept_server({"method": "notifications/tools/list_changed"})
    proxy.accept_server({"id": 2, "result": {"tools": [refund_entry(), {"name": "hidden"}]}})
    assert mcp.write_client.call_args.args[0]["result"]["tools"] == [refund_entry()]
    assert proxy.tools == {}
    await list_tools(proxy, refund_entry())
    assert "refund" in proxy.tools


async def test_first_page_replaces_cached_tools(proxy):
    await list_tools(proxy, refund_entry())
    await list_tools(proxy, cursor="page2")
    assert "refund" in proxy.tools
    await list_tools(proxy)
    assert proxy.tools == {}


async def test_stalled_sink_denies(proxy, monkeypatch):
    await list_tools(proxy, refund_entry())
    release = Event()
    started = Event()
    records = []

    def stall(record):
        started.set()
        release.wait()

    proxy.sinks = [stall, records.append]
    monkeypatch.setattr(mcp, "SINK_TIMEOUT", 0.05)
    try:
        decision = await asyncio.wait_for(proxy.decide({
            "id": 2, "params": {"name": "refund", "arguments": {"amount": 1}},
        }), 1)
        assert started.is_set()
        assert decision.reason == "decision_not_recorded"
        assert not decision.allowed
        assert len(records) == 1
    finally:
        release.set()


async def test_shutdown_kills_process_group_and_bounds_drain(monkeypatch):
    drain_cancelled = asyncio.Event()

    async def communicate():
        try:
            await asyncio.Future()
        finally:
            drain_cancelled.set()

    child = SimpleNamespace(pid=123, stdin=Mock(), communicate=communicate, returncode=0)
    spawn = Mock()

    async def create_child(*args, **kwargs):
        spawn(*args, **kwargs)
        return child

    signals = Mock()
    monkeypatch.setattr(mcp.asyncio, "create_subprocess_exec", create_child)
    monkeypatch.setattr(mcp.os, "killpg", signals)
    monkeypatch.setattr(mcp, "SHUTDOWN_TIMEOUT", 0.01)

    async def run():
        async with mcp.upstream(["server"]):
            pass

    await asyncio.wait_for(run(), 1)
    assert spawn.call_args.kwargs["start_new_session"] is True
    assert [call.args for call in signals.call_args_list] == [
        (child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL),
    ]
    child.stdin.close.assert_called_once()
    assert drain_cancelled.is_set()


async def test_decision_api_stores_and_validates_source(client):
    path = "/v1/policy/decisions"
    item = {
        "tool": "refund", "allowed": True, "reason": "granted", "policy_version": "3",
        "decided_at": "2026-01-01T00:00:00Z",
    }
    decisions = [
        dict(item, decision_id=str(uuid4()), **source)
        for source in ({}, {"source": "mcp"}, {"source": "litellm"}, {"source": "sdk"})
    ]
    response = await client.post(path, json={"decisions": decisions})
    assert response.json() == {"stored": 4, "duplicates": 0}
    records = (await client.get(path)).json()
    assert {row["decision_key"]: row["source"] for row in records} == {
        decision["decision_id"]: decision.get("source", "sdk") for decision in decisions
    }
    invalid = dict(item, decision_id=str(uuid4()), source="core")
    assert (await client.post(path, json={"decisions": [invalid]})).status_code == 422


def test_core_sink_source_and_proxy_wiring(setup_proxy, monkeypatch):
    records = []

    class Opener:
        def open(self, request, *, timeout):
            records.extend(json.loads(request.data)["decisions"])
            return nullcontext()

    record = DecisionRecord("refund", "1", True, "granted", None, "3", "2026-01-01T00:00:00Z")

    async def run_proxy(self, command):
        await list_tools(self, refund_entry())
        decision = await self.decide({"id": 2, "params": {
            "name": "refund", "arguments": {"amount": 1},
        }})
        assert decision.allowed
        return 0

    monkeypatch.setattr(sink, "build_opener", lambda _: Opener())
    monkeypatch.setattr(mcp.Proxy, "run", run_proxy)
    monkeypatch.setattr(mcp, "write_client", Mock())
    monkeypatch.setenv("X0R_API_KEY", "test-key")
    url = "https://core.example"
    sink.core_sink(url, "test-key")(record)
    policy, _, _ = setup_proxy
    result = CliRunner().invoke(app, ["mcp-proxy", "--policy", str(policy), "--core-url", url,
                                     "--", "server"])
    assert result.exit_code == 0, result.exception
    assert [row["source"] for row in records] == ["sdk", "mcp"]
