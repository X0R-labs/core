from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest
from test_policy_upload import CALCULATOR_POLICY, EMPTY_POLICY, upload
from test_sdk_wrapper import DictClient, _call, _create, _reply
from x0r_guard import DecisionNotRecorded, Policy, core_sink, sink, wrap

from x0r_core.policy import policy_tool_call_id

DECISIONS = "/v1/policy/decisions"


def decision(**overrides):
    return {
        "decision_id": str(uuid4()),
        "tool": "calculator",
        "tool_call_id": "a" * 201,
        "allowed": True,
        "reason": "granted",
        "caller_subject": "caller",
        "policy_version": "3",
        "decided_at": "2026-01-01T12:00:00+02:00",
        **overrides,
    }


async def test_post_decisions(client, workspace_factory):
    uploaded = await upload(client, CALCULATOR_POLICY)
    await upload(client, EMPTY_POLICY)
    known = decision(policy_digest=uploaded["digest"])
    unknown = decision(policy_digest="a" * 64)
    batch = {"decisions": [known, unknown, known]}
    first, second = await asyncio.gather(
        client.post(DECISIONS, json=batch), client.post(DECISIONS, json=batch),
    )
    assert first.status_code == second.status_code == 200
    assert sorted((first.json(), second.json()), key=lambda response: response["stored"]) == [
        {"stored": 0, "duplicates": 3}, {"stored": 2, "duplicates": 1},
    ]
    rows = {row["decision_key"]: row for row in (await client.get(DECISIONS)).json()}
    assert set(rows) == {known["decision_id"], unknown["decision_id"]}
    matched = rows[known["decision_id"]]
    assert matched["policy_revision"] == 1
    assert matched["policy_digest"] == uploaded["digest"]
    assert matched["tool_call_id"] == policy_tool_call_id(known["tool_call_id"])
    assert matched["decided_at"] == "2026-01-01T10:00:00Z"
    assert rows[unknown["decision_id"]]["policy_revision"] is None
    for row in rows.values():
        assert row["source"] == "sdk"
        assert row["run_id"] is row["agent_id"] is None
        assert "arguments" not in row

    _, key = await workspace_factory()
    other = {"Authorization": f"Bearer {key}"}
    assert (await client.get(DECISIONS, headers=other)).json() == []
    response = await client.post(DECISIONS, headers=other, json={"decisions": [known]})
    assert response.json() == {"stored": 1, "duplicates": 0}
    other_rows = (await client.get(DECISIONS, headers=other)).json()
    assert other_rows[0]["policy_revision"] is None


@pytest.mark.parametrize("overrides", [
    {"decision_id": "bad"},
    {"decided_at": "123456789"},
    {"policy_digest": "bad"},
    {"arguments": {"private": "value"}},
])
async def test_bad_decision_input(client, overrides):
    response = await client.post(DECISIONS, json={"decisions": [decision(**overrides)]})
    assert response.status_code == 422
    assert (await client.get(DECISIONS)).json() == []


async def test_oversized_batch(client):
    response = await client.post(DECISIONS, json={"decisions": [decision()] * 101})
    assert response.status_code == 422


async def test_core_sink_retries_against_app(client, monkeypatch):
    loop = asyncio.get_running_loop()
    requests = []
    results = []

    class Opener:
        def open(self, request, *, timeout):
            requests.append(request.data)
            response = asyncio.run_coroutine_threadsafe(
                client.post(DECISIONS, content=request.data, headers=request.headers), loop,
            ).result(timeout)
            assert response.status_code == 200, response.text
            results.append(response.json())
            if len(results) == 1:
                raise URLError("connection lost after storing")
            return nullcontext()

    monkeypatch.setattr(sink, "build_opener", lambda _: Opener())
    save = core_sink("http://localhost:8000/", client.headers["Authorization"].split()[1])
    tool = _call("lookup_order", {"order_id": "A-1"})
    guarded = wrap(DictClient(_reply(tool)), Policy(allow=["lookup_order"], on_decision=save))
    await asyncio.to_thread(_create, guarded)
    assert requests[0] == requests[1]
    item = json.loads(requests[0])["decisions"][0]
    assert "arguments" not in item
    assert results == [{"stored": 1, "duplicates": 0}, {"stored": 0, "duplicates": 1}]
    stored = (await client.get(DECISIONS)).json()
    assert [row["decision_key"] for row in stored] == [item["decision_id"]]


@pytest.mark.parametrize("status,attempts", [(401, 1), (503, 3)])
def test_sink_failure_closes_wrapper(monkeypatch, status, attempts):
    requests = []

    class Opener:
        def open(self, request, *, timeout):
            requests.append(request.data)
            raise HTTPError(request.full_url, status, "failure", {}, None)

    monkeypatch.setattr(sink, "build_opener", lambda _: Opener())
    save = core_sink("https://core.example", "fake-key")
    tool = _call("lookup_order", {"order_id": "A-1"})
    guarded = wrap(DictClient(_reply(tool)), Policy(allow=["lookup_order"], on_decision=save))
    with pytest.raises(DecisionNotRecorded) as error:
        _create(guarded)
    assert isinstance(error.value.__cause__, HTTPError)
    assert len(requests) == attempts
    assert len(set(requests)) == 1


def test_sink_refuses_remote_http():
    with pytest.raises(ValueError, match="HTTPS"):
        core_sink("http://core.example", "fake-key")


async def test_long_sdk_values_are_trimmed_not_rejected(client):
    item = decision(tool="t" * 300, reason="r" * 2000, caller_subject="c" * 500)
    response = await client.post(DECISIONS, json={"decisions": [item]})
    assert response.status_code == 200
    row = (await client.get(DECISIONS)).json()[0]
    assert (len(row["tool"]), len(row["reason"]), len(row["caller_subject"])) == (120, 1000, 200)


async def test_upload_maps_earlier_decisions_to_the_revision(client):
    from x0r_guard import load_policy_text

    digest = load_policy_text(CALCULATOR_POLICY.encode("utf-8")).digest
    await client.post(DECISIONS, json={"decisions": [decision(policy_digest=digest)]})
    assert (await client.get(DECISIONS)).json()[0]["policy_revision"] is None
    await upload(client, CALCULATOR_POLICY)
    assert (await client.get(DECISIONS)).json()[0]["policy_revision"] == 1
