from __future__ import annotations

import asyncio
from hashlib import sha256

import pytest
from test_policy_file import POLICY_CASES, RULE_POLICY
from test_sdk_wrapper import DictClient, _call, _create, _reply
from x0r_guard import Caller, Policy, check_arguments, wrap

from x0r_core.config import get_settings
from x0r_core.providers import Completion, ToolCall
from x0r_core.services import agents
from x0r_core.tools import REGISTRY, Tool

POLICY_URL = "/v1/policy"
EMPTY_POLICY = "version = 1\nallow = []\n"
CALCULATOR_POLICY = 'version = 1\nallow = ["calculator"]\n'


async def upload(client, policy):
    response = await client.put(POLICY_URL, json={"policy": policy})
    assert response.status_code == 200, response.text
    return response.json()


async def test_upload_revisions_and_isolation(client, workspace_factory):
    assert (await client.get(POLICY_URL)).status_code == 404
    error = await client.put(POLICY_URL, json={"policy": "version = 2"})
    assert error.status_code == 422
    assert "version must be the integer 1" in error.json()["detail"]
    too_large = await client.put(POLICY_URL, json={"policy": EMPTY_POLICY + "#" + "é" * 32768})
    assert too_large.status_code == 422

    source = CALCULATOR_POLICY.replace("\n", "\r\n")
    first = await upload(client, source)
    assert first["revision"] == 1
    assert first["digest"] == sha256(source.encode()).hexdigest()
    assert first["policy"] == source
    assert await upload(client, source) == first
    second = await upload(client, EMPTY_POLICY)
    assert second["revision"] == 2
    assert (await client.get(POLICY_URL)).json() == second

    _, key = await workspace_factory()
    other = {"Authorization": f"Bearer {key}"}
    assert (await client.get(POLICY_URL, headers=other)).status_code == 404
    response = await client.put(POLICY_URL, headers=other, json={"policy": EMPTY_POLICY})
    assert response.json()["revision"] == 1
    assert (await client.get(POLICY_URL)).json() == second


async def test_concurrent_uploads(client):
    first, duplicate = await asyncio.gather(
        upload(client, EMPTY_POLICY), upload(client, EMPTY_POLICY),
    )
    assert first == duplicate
    updates = await asyncio.gather(
        upload(client, CALCULATOR_POLICY), upload(client, EMPTY_POLICY + "# different\n"),
    )
    assert {row["revision"] for row in updates} == {2, 3}


class ScriptedProvider:
    name = "scripted-test"

    def __init__(self, tool, arguments, before_second=None):
        self.tool = tool
        self.arguments = arguments
        self.before_second = before_second
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        if self.calls == 2 and self.before_second:
            await self.before_second()
        elif self.calls > 1:
            return Completion(content="done")
        call = ToolCall(str(self.calls), self.tool, self.arguments)
        return Completion(tool_calls=[call])


async def run(client, monkeypatch, provider, caller=None):
    monkeypatch.setattr(agents, "build_provider", lambda _: provider)
    agent = await client.post("/v1/agents", json={"name": "test", "tools": [provider.tool]})
    assert agent.status_code in (200, 201), agent.text
    body = {"input": "Use a tool"}
    if caller is not None:
        body["caller"] = {"subject": "user", "attributes": caller}
    result = await client.post(f"/v1/agents/{agent.json()['id']}/run", json=body)
    assert result.status_code == 200, result.text
    return result.json()


async def test_run_denies_tool_missing_from_file(client, monkeypatch):
    uploaded = await upload(client, EMPTY_POLICY)
    result = await run(client, monkeypatch, ScriptedProvider("calculator", {"expression": "2+2"}))
    assert result["status"] == "blocked"
    decision = next(step for step in result["steps"] if step["type"] == "policy")
    assert decision["reason"] == "not_in_policy_file"
    assert decision["policy_digest"] == uploaded["digest"]
    assert decision["policy_revision"] == 1


async def test_run_keeps_policy_snapshot(client, monkeypatch):
    uploaded = await upload(client, CALCULATOR_POLICY)

    async def replace_policy():
        assert (await upload(client, EMPTY_POLICY))["revision"] == 2

    provider = ScriptedProvider("calculator", {"expression": "2+2"}, replace_policy)
    result = await run(client, monkeypatch, provider)
    assert result["status"] == "succeeded"
    rows = (await client.get(f"{POLICY_URL}/decisions", params={"run_id": result["id"]})).json()
    steps = [step for step in result["steps"] if step["type"] == "policy"]
    assert len(rows) == len(steps) == 2
    for decision in rows + steps:
        assert decision["allowed"]
        assert decision["policy_digest"] == uploaded["digest"]
        assert decision["policy_revision"] == 1
    for row in rows:
        assert row["source"] == "core"
        assert row["decided_at"] is not None
        assert row["decision_key"] is None


def case_schema(tool, arguments):
    properties = {
        "lookup": {"order": {"type": "string"}},
        "refund": {"amount": {"type": "number"}},
        "transfer": {"amount": {"type": "number"}},
        "ship": {"method": {"type": "number" if isinstance(arguments.get("method"), float)
                            else "string"}},
    }
    return {"type": "object", "properties": properties.get(tool, {})}


@pytest.mark.parametrize("tool,arguments,attrs,allowed,reason", [
    case for case in POLICY_CASES if check_arguments(case_schema(*case[:2]), case[1]) is None
])
async def test_core_and_sdk_policy_cases(
    client, monkeypatch, tmp_path, tool, arguments, attrs, allowed, reason,
):
    async def handler(ctx, **kwargs):
        return {}

    spec = Tool(tool, "test", case_schema(tool, arguments), handler)
    monkeypatch.setitem(REGISTRY, tool, spec)
    monkeypatch.setattr(get_settings(), "operator_tool_allowlist", tool)
    uploaded = await upload(client, RULE_POLICY.decode())
    result = await run(client, monkeypatch, ScriptedProvider(tool, arguments), attrs)
    step = next(step for step in result["steps"] if step["type"] == "policy")
    assert (step["allowed"], step["reason"]) == (allowed, reason)
    assert (step["policy_digest"], step["policy_revision"]) == (uploaded["digest"], 1)

    path = tmp_path / "policy.toml"
    path.write_bytes(RULE_POLICY)
    records = []
    policy = Policy.from_file(path, on_decision=records.append, on_denied="remove")
    caller = Caller("user", attrs) if attrs is not None else None
    guarded = wrap(DictClient(_reply(_call(tool, arguments))), policy, caller=caller)
    _create(guarded, tools=[spec.schema()])
    assert [(item.allowed, item.reason) for item in records] == [(allowed, reason)]


async def test_upload_rejects_text_that_is_not_utf8(client):
    body = '{"policy": "version = 1\\ud800"}'
    response = await client.put(
        "/v1/policy", content=body, headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
