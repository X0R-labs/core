"""Caller identity, tool-call links and decisions saved before the tool runs."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, select, text

from x0r_core.config import get_settings
from x0r_core.db import _add_missing_columns, get_session_factory
from x0r_core.guard import Caller, HookContext
from x0r_core.models import Agent, Base, PolicyDecision, Workspace
from x0r_core.providers import Completion, Provider, ToolCall
from x0r_core.services.agents import run_agent
from x0r_core.tools.base import Tool
from x0r_core.tools.builtin import REGISTRY

KB = "The synthetic warranty covers manufacturing defects for 24 months."


async def _agent(client) -> str:
    await client.post("/v1/documents", json={"title": "Warranty", "content": KB})
    agent = await client.post("/v1/agents", json={
        "name": "helper", "system_prompt": "Answer from the knowledge base.",
        "tools": ["knowledge_search"],
    })
    return agent.json()["id"]


async def test_caller_and_tool_call_id_are_recorded(client):
    agent_id = await _agent(client)
    resp = await client.post(f"/v1/agents/{agent_id}/run", json={
        "input": "How long is the synthetic warranty?",
        "caller": {"subject": "user-42", "attributes": {"role": "support"}},
    })
    assert resp.status_code == 200, resp.text
    run = resp.json()
    policy = next(s for s in run["steps"] if s["type"] == "policy")
    model = next(s for s in run["steps"] if s["type"] == "model")
    call_id = model["tool_calls"][0]["id"]
    assert policy["tool_call_id"] == call_id
    assert policy["caller_subject"] == "user-42"

    rows = (await client.get("/v1/policy/decisions", params={"tool_call_id": call_id})).json()
    assert len(rows) == 1
    assert rows[0]["run_id"] == run["id"]
    assert rows[0]["caller_subject"] == "user-42"
    assert rows[0]["tool_call_id"] == call_id
    other = (await client.get("/v1/policy/decisions", params={"tool_call_id": "nope"})).json()
    assert other == []


async def test_run_without_caller_still_works(client):
    agent_id = await _agent(client)
    resp = await client.post(f"/v1/agents/{agent_id}/run", json={"input": "warranty?"})
    assert resp.status_code == 200
    rows = (await client.get("/v1/policy/decisions")).json()
    assert rows[0]["caller_subject"] is None


async def test_blank_or_oversized_caller_is_rejected(client):
    agent_id = await _agent(client)
    for caller in ({"subject": "   "}, {"subject": ""}, {"subject": "x" * 201},
                   {"subject": "u", "attributes": {"k": "v" * 201}},
                   {"subject": "u", "attributes": {f"k{i}": "v" for i in range(21)}}):
        resp = await client.post(f"/v1/agents/{agent_id}/run",
                                 json={"input": "hi", "caller": caller})
        assert resp.status_code == 422, caller


class OrderProvider(Provider):
    name = "order-test"

    def __init__(self, order_id: str):
        self.order_id = order_id
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return Completion(content="done")
        call = ToolCall("call_1", "order_refund", {"order_id": self.order_id})
        return Completion(tool_calls=[call])


def _own_record(arguments, hook: HookContext):
    if hook.caller is None or arguments["order_id"] != hook.caller.attributes.get("order"):
        return "not_callers_record"
    return None


async def _run_refund(workspace_factory, monkeypatch, *, caller, order_id, handler):
    workspace_id, _ = await workspace_factory()
    tool = Tool(
        name="order_refund", description="Refund an order.",
        parameters={"type": "object", "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"]},
        handler=handler, authorize=_own_record,
    )
    monkeypatch.setitem(REGISTRY, tool.name, tool)
    monkeypatch.setattr(get_settings(), "operator_tool_allowlist", "order_refund")
    async with get_session_factory()() as session:
        workspace = await session.get(Workspace, workspace_id)
        agent = Agent(workspace_id=workspace_id, name="t", tools=["order_refund"], max_steps=3)
        session.add(agent)
        await session.flush()
        return await run_agent(session, workspace, agent, "refund", caller=caller,
                               provider=OrderProvider(order_id))


async def test_caller_reaches_the_tool_rule(workspace_factory, monkeypatch):
    called: list[str] = []

    async def refund(ctx, order_id):
        called.append(order_id)
        return {"ok": True}

    caller = Caller(subject="user-1", attributes={"order": "A-1"})
    run = await _run_refund(workspace_factory, monkeypatch, caller=caller, order_id="A-1",
                            handler=refund)
    assert run.status == "succeeded"
    assert called == ["A-1"]

    run = await _run_refund(workspace_factory, monkeypatch, caller=caller, order_id="B-2",
                            handler=refund)
    assert run.status == "blocked"
    assert called == ["A-1"]
    assert next(s for s in run.steps if s["type"] == "policy")["reason"] == "not_callers_record"


async def test_decision_is_saved_before_the_tool_runs(workspace_factory, monkeypatch):
    seen: list[tuple] = []

    async def refund(ctx, order_id):
        # A separate connection sees the decision while the tool is still running.
        async with get_session_factory()() as other:
            rows = (await other.execute(
                select(PolicyDecision).where(
                    PolicyDecision.workspace_id == ctx.workspace_id,
                    PolicyDecision.tool_call_id == "call_1",
                )
            )).scalars().all()
            seen.extend((r.allowed, r.reason, r.caller_subject) for r in rows)
        return {"ok": True}

    caller = Caller(subject="user-1", attributes={"order": "A-1"})
    run = await _run_refund(workspace_factory, monkeypatch, caller=caller, order_id="A-1",
                            handler=refund)
    assert run.status == "succeeded"
    assert seen == [(True, "granted", "user-1")]


async def test_permitted_tool_steps_keep_their_order(workspace_factory, monkeypatch):
    caller = Caller(subject="user-1", attributes={"order": "A-1"})

    async def refund(ctx, order_id):
        return {"ok": True}

    run = await _run_refund(workspace_factory, monkeypatch, caller=caller, order_id="A-1",
                            handler=refund)
    assert [s["type"] for s in run.steps] == ["model", "policy", "tool", "model"]


def test_old_database_gets_new_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE workspaces (id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(text(
            "CREATE TABLE policy_decisions (id INTEGER PRIMARY KEY, workspace_id VARCHAR(32), "
            "run_id VARCHAR(32), agent_id VARCHAR(64), tool VARCHAR(120), allowed BOOLEAN, "
            "reason TEXT, policy_version VARCHAR(10), created_at DATETIME)"
        ))
        conn.execute(text("INSERT INTO policy_decisions (tool, allowed, reason, policy_version) "
                          "VALUES ('calculator', 1, 'granted', '2')"))
    with engine.begin() as conn:
        _add_missing_columns(conn)
        _add_missing_columns(conn)  # running twice is harmless
    columns = {c["name"] for c in inspect(engine).get_columns("policy_decisions")}
    assert {"tool_call_id", "caller_subject"} <= columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT tool, tool_call_id FROM policy_decisions")).all() == [
            ("calculator", None)
        ]
    assert "policy_decisions" in Base.metadata.tables


def test_old_database_gets_the_tool_call_index(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE policy_decisions (id INTEGER PRIMARY KEY, tool VARCHAR(120))"
        ))
    with engine.begin() as conn:
        _add_missing_columns(conn)
    names = {i["name"] for i in inspect(engine).get_indexes("policy_decisions")}
    assert "ix_policy_decisions_tool_call_id" in names


async def test_control_characters_in_caller_are_rejected(client):
    agent_id = await _agent(client)
    for caller in ({"subject": "user\x00"}, {"subject": "u", "attributes": {"k\n": "v"}},
                   {"subject": "u", "attributes": {"k": "v\x7f"}}):
        resp = await client.post(f"/v1/agents/{agent_id}/run",
                                 json={"input": "hi", "caller": caller})
        assert resp.status_code == 422, caller


async def test_unsaved_decision_means_the_tool_never_runs(client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    agent_id = await _agent(client)
    called: list[bool] = []
    original = REGISTRY["knowledge_search"]

    async def counted(*args, **kwargs):
        called.append(True)
        return await original.handler(*args, **kwargs)

    async def failing_commit(self):
        raise RuntimeError("disk full")

    monkeypatch.setitem(REGISTRY, original.name, Tool(
        original.name, original.description, original.parameters, counted, original.authorize,
    ))
    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    resp = await client.post(f"/v1/agents/{agent_id}/run", json={"input": "warranty?"})
    assert resp.status_code == 503
    assert called == []


def test_hooks_written_for_settings_still_work():
    from x0r_core.config import Settings
    from x0r_core.policy import authorize_tool

    def old_style(arguments, settings):
        return None if "calc" in settings.operator_tool_allowlist else "denied"

    tool = Tool("calc", "", {"type": "object", "properties": {}}, None, old_style)
    decision = authorize_tool(
        workspace_id="w", agent_workspace_id="w", agent_tools=["calc"], tool_name="calc",
        arguments={}, settings=Settings(operator_tool_allowlist="calc"), registry={"calc": tool},
    )
    assert decision.allowed
