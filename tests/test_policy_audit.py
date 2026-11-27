from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from x0r_core.config import get_settings
from x0r_core.main import app

KB = "The synthetic warranty covers manufacturing defects for 24 months."


async def _run(client, question="How long is the synthetic warranty?"):
    await client.post("/v1/documents", json={"title": "Warranty", "content": KB})
    agent = await client.post("/v1/agents", json={
        "name": "helper", "system_prompt": "Answer from the knowledge base.",
        "tools": ["knowledge_search"],
    })
    resp = await client.post(f"/v1/agents/{agent.json()['id']}/run", json={"input": question})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_granted_decision_is_recorded(client):
    run = await _run(client)
    assert run["status"] == "succeeded"

    resp = await client.get("/v1/policy/decisions", params={"run_id": run["id"]})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "knowledge_search"
    assert row["allowed"] is True
    assert row["reason"] == "granted"
    assert row["policy_version"] == "3"
    assert "arguments" not in row


async def test_denied_decision_is_recorded_and_filterable(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "operator_tool_allowlist", "calculator")
    run = await _run(client)
    assert run["status"] == "blocked"

    denied = (await client.get("/v1/policy/decisions", params={"allowed": False})).json()
    assert [(r["run_id"], r["reason"]) for r in denied] == [
        (run["id"], "not_granted_by_operator")
    ]
    allowed = (await client.get("/v1/policy/decisions", params={"allowed": True})).json()
    assert allowed == []


async def test_decisions_are_workspace_scoped(client, workspace_factory):
    await _run(client)
    _, other_key = await workspace_factory("other")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {other_key}"},
    ) as other:
        resp = await other.get("/v1/policy/decisions")
        assert resp.status_code == 200
        assert resp.json() == []


async def test_decisions_require_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/v1/policy/decisions")
        assert resp.status_code == 401
