from __future__ import annotations

import pytest

from x0r_core.config import Settings, get_settings
from x0r_core.db import get_session_factory
from x0r_core.models import Agent, Workspace
from x0r_core.policy import Decision, authorize_tool
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


# ── Generic, schema-driven checks (policy v2) ───────────────────────────────

async def _noop(ctx, **kwargs):
    return {}


ORDER_TOOL = Tool(
    "order_lookup", "test",
    {"type": "object", "properties": {"order_id": {"type": "string", "maxLength": 16}},
     "required": ["order_id"]},
    _noop,
)


def custom(arguments, *, allowlist="order_lookup", tool=ORDER_TOOL):
    return authorize_tool(
        workspace_id="a", agent_workspace_id="a", agent_tools=[tool.name],
        tool_name=tool.name, arguments=arguments,
        settings=Settings(operator_tool_allowlist=allowlist), registry={tool.name: tool},
    )


def test_custom_tool_is_allowed_when_granted():
    assert custom({"order_id": "A-1001"}) == Decision(True, "granted")


@pytest.mark.parametrize("arguments,reason", [
    ({}, "invalid_arguments"),
    ({"order_id": "   "}, "invalid_arguments"),
    ({"order_id": 1001}, "invalid_arguments"),
    ({"order_id": "x" * 17}, "invalid_arguments"),
    ({"order_id": "A-1", "workspace_id": "b"}, "unexpected_arguments"),
])
def test_custom_tool_arguments_follow_its_schema(arguments, reason):
    assert custom(arguments) == Decision(False, reason)


def test_custom_tool_still_needs_operator_grant():
    assert custom({"order_id": "A-1"}, allowlist="calculator").reason == (
        "not_granted_by_operator"
    )


def test_unregistered_tool_is_denied():
    result = authorize_tool(
        workspace_id="a", agent_workspace_id="a", agent_tools=["ghost"], tool_name="ghost",
        arguments={}, settings=Settings(operator_tool_allowlist="ghost"), registry={},
    )
    assert result == Decision(False, "unknown_tool")


def test_failing_authorizer_denies():
    def broken(arguments, settings):
        raise RuntimeError("boom")

    tool = Tool("hooked", "test", {"type": "object", "properties": {}}, _noop, broken)
    assert custom({}, allowlist="hooked", tool=tool) == Decision(False, "authorizer_error")


def test_untyped_property_is_denied():
    tool = Tool("loose", "test", {"type": "object", "properties": {"x": {}}}, _noop)
    assert custom({"x": "anything"}, allowlist="loose", tool=tool).reason == "invalid_arguments"


@pytest.mark.parametrize("k", [0, 11, True, 2.0])
def test_knowledge_search_k_bounds(k):
    result = decision(agent_tools=["knowledge_search"], tool_name="knowledge_search",
                      arguments={"query": "warranty", "k": k},
                      settings=Settings(operator_tool_allowlist="knowledge_search"))
    assert result == Decision(False, "invalid_arguments")


@pytest.mark.parametrize("url,allowed", [
    ("https://example.com/a", True),
    ("https://evil.test/a", False),
    ("https://user:pw@example.com/", False),
    ("https://example.com:8443/", False),
    ("ftp://example.com/", False),
])
def test_http_get_destination_hook(url, allowed):
    result = decision(agent_tools=["http_get"], tool_name="http_get", arguments={"url": url},
                      settings=Settings(operator_tool_allowlist="http_get",
                                        http_tool_allowlist="example.com"))
    assert result.allowed is allowed
    if not allowed:
        assert result.reason == "http_destination_denied"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_denied(value):
    tool = Tool("ratio", "test", {"type": "object", "properties": {
        "x": {"type": "number", "minimum": 0, "maximum": 1}}, "required": ["x"]}, _noop)
    assert custom({"x": value}, allowlist="ratio", tool=tool) == Decision(
        False, "invalid_arguments")


def test_long_custom_reason_is_kept_as_text():
    long_reason = "denied_by_" + "x" * 200
    tool = Tool("strict", "test", {"type": "object", "properties": {}}, _noop,
                lambda arguments, settings: long_reason)
    assert custom({}, allowlist="strict", tool=tool) == Decision(False, long_reason)


def test_non_string_reason_is_coerced():
    tool = Tool("odd", "test", {"type": "object", "properties": {}}, _noop,
                lambda arguments, settings: 403)
    assert custom({}, allowlist="odd", tool=tool) == Decision(False, "403")
