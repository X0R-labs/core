from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from x0r_core.config import Settings
from x0r_core.schemas import AgentIn, DocumentIn, EvalSuiteIn, RunIn, SearchIn
from x0r_core.tools import builtin


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.consumed += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def context(**settings):
    return SimpleNamespace(settings=Settings(http_tool_allowlist="example.com", **settings))


def http_fixture(monkeypatch, stream, headers=None, status=200, public=True):
    requests = []
    options = []
    original_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers or {}, stream=stream)

    def client(**kwargs):
        options.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(builtin.httpx, "AsyncClient", client)
    monkeypatch.setattr(builtin, "_is_public_host", lambda host: public)
    return requests, options


async def test_http_small_text_is_returned_without_proxy_or_redirects(monkeypatch):
    stream = Chunks([b"<p>Hello</p>", b" world"])
    requests, options = http_fixture(monkeypatch, stream)
    result = await builtin.http_get(context(), "https://example.com/page")
    assert result["text"] == "Hello world"
    assert requests[0].headers["accept-encoding"] == "identity"
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert stream.closed


async def test_http_stream_stops_before_reading_entire_oversized_body(monkeypatch):
    stream = Chunks([b"a" * 800, b"b" * 800, b"c" * 800])
    http_fixture(monkeypatch, stream)
    result = await builtin.http_get(context(max_http_response_bytes=1024), "https://example.com")
    assert "byte limit" in result["error"]
    assert stream.consumed == 2
    assert stream.closed


@pytest.mark.parametrize("headers,status,error", [
    ({"content-length": "2048"}, 200, "byte limit"),
    ({"content-encoding": "gzip"}, 200, "compressed"),
    ({"location": "http://127.0.0.1"}, 302, "redirect"),
    ({"content-length": "not-a-number"}, 200, "content length"),
])
async def test_http_rejects_before_consuming_body(monkeypatch, headers, status, error):
    stream = Chunks([b"secret"])
    requests, _ = http_fixture(monkeypatch, stream, headers, status)
    result = await builtin.http_get(context(max_http_response_bytes=1024), "https://example.com")
    assert error in result["error"]
    assert stream.consumed == 0
    assert len(requests) == 1
    assert stream.closed


async def test_http_total_deadline_closes_stream(monkeypatch):
    stream = Chunks([b"slow"], delay=0.05)
    http_fixture(monkeypatch, stream)
    result = await builtin.http_get(context(http_tool_timeout=0.01), "https://example.com")
    assert "time limit" in result["error"]
    assert stream.closed


async def test_http_non_public_resolution_never_connects(monkeypatch):
    stream = Chunks([b"no"])
    requests, options = http_fixture(monkeypatch, stream, public=False)
    result = await builtin.http_get(context(), "https://example.com")
    assert "non-public" in result["error"]
    assert requests == options == []


@pytest.mark.parametrize("url", [
    "https://user:password@example.com/", "https://example.com:8080/", "file:///etc/passwd",
])
async def test_http_invalid_destination_never_connects(monkeypatch, url):
    requests, options = http_fixture(monkeypatch, Chunks([b"no"]))
    assert "error" in await builtin.http_get(context(), url)
    assert requests == options == []


@pytest.mark.parametrize("expression", [
    "1+" * 300 + "1", "1" * 4097, "1e100*1e100", "True", "1e309",
    "round(1.234, ndigits=2)",
])
async def test_calculator_rejects_unbounded_or_unsupported_inputs(expression):
    assert "error" in await builtin.calculator(None, expression)


async def test_calculator_still_handles_normal_arithmetic():
    assert (await builtin.calculator(None, "sqrt(16) + 2**3"))["result"] == 12


@pytest.mark.parametrize("model,payload", [
    (DocumentIn, {"title": "test", "content": "x" * 1_000_001}),
    (RunIn, {"input": "x" * 32_001}),
    (SearchIn, {"query": "x" * 8001}),
    (AgentIn, {"name": "test", "system_prompt": "x" * 32_001}),
    (EvalSuiteIn, {"agent_id": "a", "cases": [{"input": "x"}] * 101}),
])
def test_json_fields_have_explicit_limits(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("values", [
    {"max_tool_calls_per_step": 0}, {"max_upload_bytes": -1}, {"http_tool_timeout": 0},
    {"max_http_response_bytes": 1024 * 1024 * 100},
])
def test_invalid_resource_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        Settings(**values)


@pytest.mark.parametrize("overrides,status", [
    ({}, "blocked"), ({"max_steps": 1}, "limit_exceeded"),
])
async def test_non_successful_run_cannot_pass_eval(client, monkeypatch, overrides, status):
    from x0r_core.config import get_settings

    if status == "blocked":
        monkeypatch.setattr(get_settings(), "operator_tool_allowlist", "")
    agent = (await client.post("/v1/agents", json={"name": "test", **overrides})).json()
    suite = (await client.post("/v1/evals", json={
        "agent_id": agent["id"], "cases": [{"input": "hello", "expected": "Stopped"}],
    })).json()
    result = await client.post(f"/v1/evals/{suite['id']}/run", json={"threshold": 0})
    assert result.status_code == 200
    body = result.json()
    assert body["passed"] == 0
    assert body["results"][0]["status"] == status
    assert body["results"][0]["score"] == 0


async def test_eval_provider_failure_is_recorded_as_failed_case(client, monkeypatch):
    from x0r_core.providers.mock import MockProvider

    async def fail(self, **kwargs):
        raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(MockProvider, "complete", fail)
    agent = (await client.post("/v1/agents", json={"name": "test"})).json()
    suite = (await client.post("/v1/evals", json={
        "agent_id": agent["id"], "cases": [{"input": "hello", "expected": ""}],
    })).json()
    result = await client.post(f"/v1/evals/{suite['id']}/run", json={"threshold": 0})
    assert result.status_code == 200
    assert result.json()["results"][0]["status"] == "failed"
    assert result.json()["passed"] == 0
    runs = (await client.get("/v1/runs")).json()
    assert len(runs) == 1 and runs[0]["status"] == "failed"


async def test_eval_quota_stop_preserves_previous_runs_and_usage(workspace_factory):
    from httpx import ASGITransport, AsyncClient

    from x0r_core.main import app

    _, raw = await workspace_factory("eval-budget", quota=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {raw}"}) as client:
        agent = (await client.post("/v1/agents", json={"name": "test"})).json()
        suite = (await client.post("/v1/evals", json={
            "agent_id": agent["id"], "cases": [{"input": "one"}, {"input": "two"}],
        })).json()
        result = await client.post(f"/v1/evals/{suite['id']}/run", json={})
        assert result.status_code == 429
        assert len((await client.get("/v1/runs")).json()) == 1
        assert (await client.get("/v1/usage")).json()["llm_tokens_used"] > 0
