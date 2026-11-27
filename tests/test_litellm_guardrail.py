"""Exercise the proxy hooks with LiteLLM's chat response types."""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_policy_file import POLICY_CASES, RULE_POLICY
from test_sdk_wrapper import DictClient, _call, _reply
from x0r_guard import Caller, Policy, PolicyFileError, load_policy_file, wrap
from x0r_guard.identity import audience_key, sign_caller

pytest.importorskip("litellm")

import yaml  # noqa: E402
from litellm import ModelResponse  # noqa: E402
from litellm.proxy.types_utils.utils import get_instance_fn  # noqa: E402
from litellm.types.guardrails import GuardrailEventHooks, LitellmParams  # noqa: E402

from x0r_core.connectors import litellm as connector  # noqa: E402

ROOT = b"test root key".ljust(32, b"!")
KEY = audience_key(ROOT, "litellm")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    policy = tmp_path / "policy.toml"
    policy.write_bytes(RULE_POLICY)
    log = tmp_path / "decisions.jsonl"
    for name in (
        "POLICY_FILE", "DECISION_LOG", "CORE_URL", "API_KEY", "ON_DENIED",
        "CALLER_KEY", "CALLER_AUDIENCE",
    ):
        monkeypatch.delenv(f"X0R_{name}", raising=False)
    monkeypatch.setenv("X0R_POLICY_FILE", str(policy))
    monkeypatch.setenv("X0R_DECISION_LOG", str(log))
    monkeypatch.setenv("X0R_CALLER_KEY", base64.urlsafe_b64encode(KEY).decode().rstrip("="))
    return policy, log


def tool_spec(name, properties=None):
    return {
        "type": "function",
        "function": {
            "name": name, "parameters": {"type": "object", "properties": properties or {}},
        },
    }


def records(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def schema_for_call(call):
    function = call["function"]
    types = {str: "string", bool: "boolean", int: "number", float: "number"}
    properties = {
        name: {"type": types[type(value)]}
        for name, value in json.loads(function["arguments"]).items()
    }
    return tool_spec(function["name"], properties)


async def checked_reply(guard, *calls, token=None):
    headers = {"X-X0R-Caller": token} if token is not None else {}
    request = {
        "tools": [schema_for_call(call) for call in calls],
        "proxy_server_request": {"headers": headers},
    }
    await guard.async_pre_call_hook(None, None, request, "acompletion")
    response = ModelResponse(**_reply(*calls))
    return await guard.async_post_call_success_hook(request, None, response)


@pytest.mark.parametrize("tool,arguments,attrs,allowed,reason", POLICY_CASES)
async def test_wrap_agreement(setup, tool, arguments, attrs, allowed, reason):
    policy, log = setup
    caller = Caller("test-user", attrs) if attrs is not None else None
    token = sign_caller(caller, key_id="test", secret=ROOT, audience="litellm") if caller else None
    call = _call(tool, arguments)

    sdk_records = []
    inner = DictClient(_reply(call))
    wrapped = wrap(
        inner, Policy.from_file(policy, on_denied="remove", on_decision=sdk_records.append),
        caller=caller,
    )
    wrapped.chat.completions.create(tools=[schema_for_call(call)])

    guard = connector.X0RGuardrail(on_denied="remove")
    result = await checked_reply(guard, call, token=token)
    choice = result.choices[0]
    assert bool(choice.message.tool_calls) == allowed
    assert choice.finish_reason == ("tool_calls" if allowed else "stop")
    if allowed:
        assert choice.message.tool_calls[0].id == call["id"]
    decision, = records(log)
    # Schema validation denies nonfinite numbers before a file rule can run.
    if any(isinstance(value, float) and not math.isfinite(value) for value in arguments.values()):
        reason = "invalid_arguments"
    assert (decision["allowed"], decision["reason"]) == (allowed, reason)
    assert (sdk_records[0].allowed, sdk_records[0].reason) == (allowed, reason)
    assert decision["source"] == "litellm"
    assert decision["policy_digest"] == load_policy_file(policy).digest
    assert decision["caller_subject"] == (caller.subject if caller else None)


async def test_default_denial_reports_tool_and_reason(setup):
    guard = connector.X0RGuardrail()
    with pytest.raises(HTTPException) as error:
        await checked_reply(guard, _call("refund", {"amount": 51, "note": "private"}))
    assert error.value.status_code == 400
    assert error.value.detail == "Tool call denied: refund (rule_max)"
    assert records(setup[1])[0]["allowed"] is False


async def test_remove_keeps_allowed_calls(setup):
    guard = connector.X0RGuardrail(on_denied="remove")
    response = await checked_reply(
        guard, _call("refund", {"amount": 50}, "ok"),
        _call("outside_policy", {}, "blocked"),
    )
    choice = response.choices[0]
    assert [call.id for call in choice.message.tool_calls] == ["ok"]
    assert choice.finish_reason == "tool_calls"
    assert [entry["allowed"] for entry in records(setup[1])] == [True, False]


@pytest.mark.parametrize("token,reason", [
    ("broken", "caller_token_malformed"),
    (sign_caller(Caller("user"), key_id="test", secret=ROOT, audience="mcp"),
     "caller_signature_invalid"),
])
async def test_bad_caller_denies_every_call(setup, token, reason):
    guard = connector.X0RGuardrail(on_denied="remove")
    response = await checked_reply(
        guard, _call("refund", {"amount": 10}, "one"), _call("ship", {}, "two"),
        token=token,
    )
    assert response.choices[0].message.tool_calls is None
    assert [(entry["allowed"], entry["reason"]) for entry in records(setup[1])] == [
        (False, reason), (False, reason),
    ]


@pytest.mark.parametrize("data,detail", [
    ({"stream": True}, "stream=false"),
    ({"functions": [{"name": "refund"}]}, "tools="),
    ({"function_call": "auto"}, "tools="),
    ({"extra_body": {"stream": True}}, "outside extra_body"),
])
async def test_pre_call_refuses_unsupported_requests(setup, data, detail):
    guard = connector.X0RGuardrail()
    with pytest.raises(HTTPException) as error:
        await guard.async_pre_call_hook(None, None, data, "acompletion")
    assert error.value.status_code == 400
    assert detail in error.value.detail


async def test_schema_snapshot(setup):
    guard = connector.X0RGuardrail()
    tool = tool_spec("refund")
    schema = tool["function"]["parameters"]
    schema["properties"] = {"amount": {"type": "number"}}
    request = {"tools": [tool]}
    await guard.async_pre_call_hook(None, None, request, "acompletion")
    schema["properties"]["amount"]["type"] = "string"
    response = ModelResponse(**_reply(_call("refund", {"amount": 10})))
    assert await guard.async_post_call_success_hook(request, None, response) is response
    assert records(setup[1])[0]["allowed"] is True


async def test_sink_failure_withholds_allowed_reply(setup, tmp_path):
    guard = connector.X0RGuardrail(decision_log=str(tmp_path / "missing" / "decisions"))
    with pytest.raises(HTTPException) as error:
        await checked_reply(guard, _call("refund", {"amount": 10}))
    assert error.value.status_code == 503


async def test_core_sink_source_and_digest(setup, monkeypatch):
    received = []

    def core_sink(url, key, *, source):
        assert (url, key, source) == ("https://core.example", "key", "litellm")
        return received.append

    monkeypatch.setattr(connector, "core_sink", core_sink)
    guard = connector.X0RGuardrail(core_url="https://core.example", api_key="key")
    await checked_reply(guard, _call("refund", {"amount": 10}))
    assert received[0].policy_digest == records(setup[1])[0]["policy_digest"]
    assert received[0].decision_id == records(setup[1])[0]["decision_id"]


def test_startup_errors(setup, monkeypatch, tmp_path):
    monkeypatch.delenv("X0R_DECISION_LOG")
    with pytest.raises(ValueError, match="X0R_DECISION_LOG"):
        connector.X0RGuardrail()
    with pytest.raises(PolicyFileError, match="missing.toml"):
        connector.X0RGuardrail(policy_file=str(tmp_path / "missing.toml"))


def test_example_registers_both_hooks(setup):
    config = Path(__file__).resolve().parents[1] / "examples" / "litellm" / "config.yaml"
    entry, = yaml.safe_load(config.read_text())["guardrails"]
    params = LitellmParams(**entry["litellm_params"]).model_dump(exclude_none=True)
    guardrail_class = get_instance_fn(params.pop("guardrail"), config_file_path=str(config))
    guard = guardrail_class(
        guardrail_name=entry["guardrail_name"], event_hook=params.pop("mode"), **params,
    )
    assert guard.should_run_guardrail({"stream": True}, GuardrailEventHooks.pre_call)
    assert guard.should_run_guardrail({"stream": False}, GuardrailEventHooks.post_call)


@pytest.mark.parametrize("call_type", ["aresponses", "anthropic_messages", "atext_completion"])
async def test_unchecked_call_types_are_refused(setup, call_type):
    guard = connector.X0RGuardrail()
    with pytest.raises(HTTPException) as raised:
        await guard.async_pre_call_hook(None, None, {"tools": []}, call_type)
    assert raised.value.status_code == 400


async def test_embeddings_pass_through(setup):
    guard = connector.X0RGuardrail()
    assert await guard.async_pre_call_hook(None, None, {"input": "hi"}, "aembedding") is None
