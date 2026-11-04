from __future__ import annotations

import pytest

from x0r_core.config import Settings, get_settings
from x0r_core.db import get_session_factory
from x0r_core.models import Agent, Workspace
from x0r_core.policy import authorize_tool
from x0r_core.providers import Completion, Provider, ToolCall
from x0r_core.providers.mock import MockProvider
from x0r_core.services.agents import run_agent
from x0r_core.services.ingest import ingest_document
from x0r_core.tools import REGISTRY
from x0r_core.tools.base import Tool


def decision(**overrides):
    values = dict(workspace_id="a", agent_workspace_id="a", agent_tools=["calculator"],
                  tool_name="calculator", arguments={"expression": "2+2"}, settings=Settings())
    values.update(overrides)
    return authorize_tool(**values)


def test_allowed_arguments():
    assert decision().allowed


@pytest.mark.parametrize("overrides,reason", [
    ({"agent_workspace_id": "b"}, "workspace_mismatch"),
    ({"agent_tools": []}, "not_granted_to_agent"),
    ({"settings": Settings(operator_tool_allowlist="")}, "not_granted_by_operator"),
    ({"arguments": {"expression": "2+2", "workspace_id": "b"}}, "unexpected_arguments"),
    ({"arguments": []}, "invalid_arguments"),
])
def test_denial_reasons(overrides, reason):
    result = decision(**overrides)
    assert not result.allowed
    assert result.reason == reason


def test_http_requires_explicit_operator_grant():
    assert not decision(agent_tools=["http_get"], tool_name="http_get",
                        arguments={"url": "https://example.com"}).allowed


class ScriptedProvider(Provider):
    name = "scripted-test"

    def __init__(self, tool="calculator", count=1):
        self.tool = tool
        self.count = count

    async def complete(self, **kwargs):
        return Completion(tool_calls=[
            ToolCall(str(i), self.tool, {"expression": "2+2"}) for i in range(self.count)
        ])


@pytest.mark.parametrize("tool_name", ["dangerous", "knowledge_search"])
async def test_denied_tool_never_executes(workspace_factory, monkeypatch, tool_name):
    workspace_id, _ = await workspace_factory()
    called = []

    async def dangerous(*args, **kwargs):
        called.append(True)
        return {}

    monkeypatch.setitem(REGISTRY, tool_name, Tool(tool_name, "test", {}, dangerous))
    monkeypatch.setattr(get_settings(), "operator_tool_allowlist", "calculator")
    async with get_session_factory()() as session:
        workspace = await session.get(Workspace, workspace_id)
        agent = Agent(workspace_id=workspace_id, name="test", tools=[tool_name], max_steps=2)
        session.add(agent)
        await session.flush()
        provider = (
            MockProvider() if tool_name == "knowledge_search" else ScriptedProvider(tool_name)
        )
        run = await run_agent(session, workspace, agent, "Request a tool lookup",
                              provider=provider)
        assert run.status == "blocked"
        assert called == []
        assert any(s.get("reason") == "not_granted_by_operator" for s in run.steps)
        policy = next(s for s in run.steps if s["type"] == "policy")
        assert "arguments" not in policy
        assert not any(s["type"] == "tool" for s in run.steps)


async def test_permitted_mock_tool_executes_once(workspace_factory, monkeypatch):
    from dataclasses import replace

    workspace_id, _ = await workspace_factory()
    original = REGISTRY["knowledge_search"]
    called = []

    async def counted(*args, **kwargs):
        called.append(True)
        return await original.handler(*args, **kwargs)

    monkeypatch.setitem(REGISTRY, original.name, replace(original, handler=counted))
    monkeypatch.setattr(get_settings(), "operator_tool_allowlist", "knowledge_search,calculator")
    async with get_session_factory()() as session:
        workspace = await session.get(Workspace, workspace_id)
        await ingest_document(
            session, workspace, title="Synthetic warranty",
            content="The synthetic warranty covers manufacturing defects for 24 months.",
            provider=MockProvider(), embed=False,
        )
        agent = Agent(workspace_id=workspace_id, name="test", tools=[original.name], max_steps=2)
        session.add(agent)
        await session.flush()
        run = await run_agent(session, workspace, agent, "How long is the synthetic warranty?",
                              provider=MockProvider())
        assert run.status == "succeeded"
        assert called == [True]
        assert "24 months" in run.output
        assert [s["type"] for s in run.steps] == ["model", "policy", "tool", "model"]
        assert next(s for s in run.steps if s["type"] == "policy")["reason"] == "granted"


@pytest.mark.parametrize("count,reason", [(1, "step_limit"), (9, "tool_call_limit")])
async def test_limits_are_not_success(workspace_factory, count, reason):
    workspace_id, _ = await workspace_factory()
    async with get_session_factory()() as session:
        workspace = await session.get(Workspace, workspace_id)
        agent = Agent(workspace_id=workspace_id, name="test", tools=["calculator"], max_steps=1)
        session.add(agent)
        await session.flush()
        run = await run_agent(
            session, workspace, agent, "calculate", provider=ScriptedProvider(count=count)
        )
        assert run.status == "limit_exceeded"
        assert any(s["type"] == "limit" for s in run.steps)
        if reason == "tool_call_limit":
            assert not any(s["type"] == "tool" for s in run.steps)


async def test_upload_limit_rejects_oversize(client, monkeypatch):
    from x0r_core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_bytes", 8)
    response = await client.post(
        "/v1/documents/upload", files={"file": ("test.txt", b"123456789", "text/plain")}
    )
    assert response.status_code == 413
