from __future__ import annotations


async def test_health_is_public(anon_client):
    resp = await anon_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["provider"] == "mock"


async def test_root_lists_endpoints(anon_client):
    resp = await anon_client.get("/")
    assert resp.status_code == 200
    assert resp.json()["api"] == "/v1"


async def test_missing_key_is_401(anon_client):
    resp = await anon_client.get("/v1/me")
    assert resp.status_code == 401
    assert "Missing API key" in resp.json()["detail"]


async def test_malformed_key_is_401(anon_client):
    resp = await anon_client.get("/v1/me", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


async def test_wrong_key_is_401(anon_client):
    resp = await anon_client.get(
        "/v1/me", headers={"Authorization": "Bearer x0r_deadbeef_wrong"}
    )
    assert resp.status_code == 401


async def test_me_returns_workspace(client):
    resp = await client.get("/v1/me")
    assert resp.status_code == 200
    assert resp.json()["plan"] == "test"


async def test_x_api_key_header_works(anon_client, workspace_factory):
    _, raw = await workspace_factory("header-test")
    resp = await anon_client.get("/v1/me", headers={"X-API-Key": raw})
    assert resp.status_code == 200


async def test_workspace_creation_requires_bootstrap(anon_client):
    resp = await anon_client.post("/v1/workspaces", json={"name": "nope"})
    assert resp.status_code == 403

    ok = await anon_client.post(
        "/v1/workspaces",
        json={"name": "acme", "monthly_token_quota": 5000},
        headers={"X-Bootstrap-Token": "test-bootstrap"},
    )
    assert ok.status_code == 201
    body = ok.json()
    assert body["api_key"].startswith("x0r_")
    assert body["workspace"]["name"] == "acme"
