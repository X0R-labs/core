from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from x0r_core.main import app

KB = (
    "Refunds are available within 30 days of delivery. After 30 days, refunds are prorated at "
    "50 percent up to day 60.\n\n"
    "Express delivery takes 1 to 2 business days."
)


async def _agent(client, tools=None, **overrides):
    payload = {
        "name": "helper",
        "system_prompt": "Answer from the knowledge base.",
        "tools": tools if tools is not None else ["knowledge_search"],
        **overrides,
    }
    resp = await client.post("/v1/agents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_agent_runs_and_uses_knowledge_tool(client):
    await client.post("/v1/documents", json={"title": "Policy", "content": KB})
    agent = await _agent(client)

    resp = await client.post(
        f"/v1/agents/{agent['id']}/run",
        json={"input": "Can I get a refund after 45 days?"},
    )
    assert resp.status_code == 200, resp.text
    run = resp.json()

    assert run["status"] == "succeeded"
    assert "prorated" in run["output"].lower()
    assert [s["type"] for s in run["steps"]][0] == "model"
    assert any(s["type"] == "tool" and s["tool"] == "knowledge_search" for s in run["steps"])


async def test_run_is_persisted_and_listed(client):
    agent = await _agent(client)
    resp = await client.post(f"/v1/agents/{agent['id']}/run", json={"input": "hello"})
    run_id = resp.json()["id"]

    listed = await client.get("/v1/runs")
    assert any(r["id"] == run_id for r in listed.json())

    fetched = await client.get(f"/v1/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == run_id


async def test_unknown_agent_is_404(client):
    resp = await client.post("/v1/agents/agt_missing/run", json={"input": "hi"})
    assert resp.status_code == 404


async def test_unknown_tool_is_reported_not_fatal(client):
    agent = await _agent(client, tools=["does_not_exist"])
    resp = await client.post(f"/v1/agents/{agent['id']}/run", json={"input": "hi"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"


async def test_agent_is_workspace_scoped(client, workspace_factory):
    agent = await _agent(client)
    _, other_key = await workspace_factory("other")

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_key}"},
    ) as other:
        resp = await other.get(f"/v1/agents/{agent['id']}")
        assert resp.status_code == 404


async def test_quota_exceeded_returns_429(workspace_factory):
    _, raw = await workspace_factory("tiny-quota", quota=1)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw}"},
    ) as c:
        agent = (await c.post("/v1/agents", json={"name": "a"})).json()
        first = await c.post(f"/v1/agents/{agent['id']}/run", json={"input": "hi"})
        assert first.status_code == 200

        second = await c.post(f"/v1/agents/{agent['id']}/run", json={"input": "hi again"})
        assert second.status_code == 429
        assert second.headers["x-quota-limit"] == "1"


async def test_usage_endpoint_tracks_tokens(client):
    agent = await _agent(client)
    await client.post(f"/v1/agents/{agent['id']}/run", json={"input": "hello there"})

    usage = (await client.get("/v1/usage")).json()
    assert usage["llm_tokens_used"] > 0
    assert usage["monthly_token_quota"] == 1_000_000
    assert usage["remaining"] < usage["monthly_token_quota"]
