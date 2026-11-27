"""Exercise the installed CLI and a real child process over stdio."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from x0r_guard.engine import Caller
from x0r_guard.identity import audience_key, sign_caller

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
