"""Signed caller tokens: the guard functions and Core's key and token endpoints."""
from __future__ import annotations

import base64
import json

import pytest
from httpx import ASGITransport, AsyncClient

from x0r_core.guard import (
    Caller,
    CallerTokenError,
    audience_key,
    sign_caller,
    token_key_id,
    verify_caller,
)
from x0r_core.main import app

SECRET = b"s" * 32
OTHER = b"o" * 32
NOW = 1_800_000_000


def _sign(caller=None, **kwargs):
    kwargs.setdefault("key_id", "ck_1")
    kwargs.setdefault("secret", SECRET)
    kwargs.setdefault("audience", "mcp-proxy")
    kwargs.setdefault("now", NOW)
    return sign_caller(caller or Caller("user-42", {"orders": ("A-1", "A-2")}), **kwargs)


def _verify(token, **kwargs):
    kwargs.setdefault("keys", {"ck_1": audience_key(SECRET, "mcp-proxy")})
    kwargs.setdefault("audience", "mcp-proxy")
    kwargs.setdefault("now", NOW + 10)
    return verify_caller(token, **kwargs)


def _reason(token, **kwargs) -> str:
    with pytest.raises(CallerTokenError) as info:
        _verify(token, **kwargs)
    return info.value.reason


def _resign(token: str, change) -> str:
    """Edit a token's payload and sign it again with the real secret."""
    import hashlib
    import hmac

    _, _, body, _ = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    change(payload)
    try:  # unescaped UTF-8 JSON, unless a lone surrogate forces escapes
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raw = json.dumps(payload).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signed = f"x0r.v1.{body}"
    sig = hmac.new(audience_key(SECRET, "mcp-proxy"), signed.encode(), hashlib.sha256).digest()
    return f"{signed}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


# ── guard functions ──────────────────────────────────────────────────────────

def test_round_trip_keeps_subject_and_list_attributes():
    caller = _verify(_sign())
    assert caller == Caller("user-42", {"orders": ("A-1", "A-2")})


def test_rotation_accepts_any_listed_key():
    token = _sign(key_id="ck_old", secret=OTHER)
    keys = {"ck_new": audience_key(SECRET, "mcp-proxy"), "ck_old": audience_key(OTHER, "mcp-proxy")}
    assert _verify(token, keys=keys).subject == "user-42"
    assert _reason(token, keys={"ck_new": keys["ck_new"]}) == "caller_signature_invalid"


def test_tampered_payload_or_signature_is_rejected():
    token = _sign()
    prefix, version, body, sig = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged["sub"] = "admin"
    body2 = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    assert _reason(f"{prefix}.{version}.{body2}.{sig}") == "caller_signature_invalid"
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    assert _reason(f"{prefix}.{version}.{body}.{flipped}") == "caller_signature_invalid"
    assert _reason(token, keys={"ck_1": audience_key(OTHER, "mcp-proxy")}) \
        == "caller_signature_invalid"


def test_expiry_audience_and_clock_skew():
    token = _sign(ttl=60)
    assert _verify(token, now=NOW + 60 + 29).subject == "user-42"   # inside the leeway
    assert _reason(token, now=NOW + 60 + 30) == "caller_expired"
    assert _reason(token, audience="x0r-core") == "caller_wrong_audience"
    # A verifier key for one audience can't check (or make) tokens for another.
    core_token = _sign(audience="x0r-core")
    assert _reason(core_token, audience="x0r-core") == "caller_signature_invalid"
    assert audience_key(SECRET, "a") != audience_key(SECRET, "b")
    assert _reason(token, now=NOW - 31) == "caller_token_malformed"   # issued in the future


@pytest.mark.parametrize("token", [
    None, 42, "", "x0r.v1", "x0r.v2.a.b", "jwt.header.body.sig", "x0r.v1.!!!.abc",
    "x0r.v1.e30.", "x0r.v1." + "A" * 9000 + ".x",
    "x0r.v1." + base64.urlsafe_b64encode(b"[" * 5000 + b"]" * 5000).decode().rstrip("=") + ".AA",
])
def test_malformed_tokens(token):
    assert _reason(token) in {"caller_token_malformed", "caller_signature_invalid"}


@pytest.mark.parametrize("change", [
    lambda p: p.pop("exp"),
    lambda p: p.update(extra=1),
    lambda p: p.update(exp=p["iat"]),
    lambda p: p.update(exp=p["iat"] + 2 * 24 * 3600),
    lambda p: p.update(iat=float(p["iat"])),
    lambda p: p.update(sub=""),
    lambda p: p.update(sub="a\nb"),
    lambda p: p.update(sub="   "),
    lambda p: p.update(attrs={"orders": [1, 2]}),
    lambda p: p.update(attrs={"orders": {"x": "y"}}),
    lambda p: p.update(attrs={f"k{i}": "v" for i in range(21)}),
])
def test_signed_but_invalid_payloads_are_rejected(change):
    assert _reason(_resign(_sign(), change)) == "caller_token_malformed"


@pytest.mark.parametrize("kwargs", [
    {"ttl": 0}, {"ttl": 24 * 3600 + 1}, {"ttl": True}, {"secret": b"short"},
    {"key_id": ""}, {"audience": "a\nb"},
])
def test_signing_rejects_bad_settings(kwargs):
    with pytest.raises(ValueError):
        _sign(**kwargs)


def test_signing_rejects_bad_callers():
    with pytest.raises(TypeError):
        sign_caller("user-42", key_id="k", secret=SECRET, audience="a")
    with pytest.raises(ValueError):
        _sign(Caller("user-42", {"orders": ("ok", "bad\x00")}))


def test_unknown_key_id_and_key_id_lookup():
    assert _reason(_sign(key_id="ck_x")) == "caller_signature_invalid"
    assert token_key_id(_sign(key_id="ck_9")) == "ck_9"
    with pytest.raises(CallerTokenError):
        token_key_id("x0r.v1.e30.AA")


# ── Core endpoints ───────────────────────────────────────────────────────────

async def _agent(client) -> str:
    await client.post("/v1/documents", json={"title": "Warranty", "content": "24 months."})
    agent = await client.post("/v1/agents", json={
        "name": "helper", "system_prompt": "Answer.", "tools": ["knowledge_search"],
    })
    return agent.json()["id"]


async def _token(client, **body) -> str:
    body.setdefault("caller", {"subject": "user-42", "attributes": {"orders": ["A-1"]}})
    resp = await client.post("/v1/caller-tokens", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def test_mint_and_use_a_token_for_a_run(client):
    assert (await client.post("/v1/caller-tokens", json={
        "caller": {"subject": "u"}})).status_code == 409
    key = (await client.post("/v1/caller-keys", json={"name": "proxy"})).json()
    assert "secret" not in key and "salt" not in key and key["signing"] is True
    listed = (await client.get("/v1/caller-keys")).json()
    assert [k["id"] for k in listed] == [key["id"]] and "secret" not in listed[0]

    agent_id = await _agent(client)
    token = await _token(client)
    resp = await client.post(f"/v1/agents/{agent_id}/run",
                             json={"input": "warranty?", "caller_token": token})
    assert resp.status_code == 200, resp.text
    rows = (await client.get("/v1/policy/decisions")).json()
    assert rows[0]["caller_subject"] == "user-42"

    # A proxy gets a verify key for its own audience only.
    verifier = (await client.post(f"/v1/caller-keys/{key['id']}/verifier",
                                  json={"audience": "mcp-proxy"})).json()
    proxy_key = base64.urlsafe_b64decode(verifier["key"] + "=")
    proxy_token = await _token(client, audience="mcp-proxy")
    assert verify_caller(proxy_token, keys={key["id"]: proxy_key}, audience="mcp-proxy") \
        .attributes == {"orders": ("A-1",)}
    # ...and that key can't stand in for Core's: a token it signs is refused by Core.
    forged = sign_caller(Caller("admin"), key_id=key["id"], secret=b"x" * 32,
                         audience="x0r-core")
    resp = await client.post(f"/v1/agents/{agent_id}/run",
                             json={"input": "x", "caller_token": forged})
    assert resp.status_code == 401
    assert (await client.post(f"/v1/caller-keys/{key['id']}/verifier",
                              json={"audience": "x0r-core"})).status_code == 422


async def test_mint_rejects_bad_claims_with_422(client):
    await client.post("/v1/caller-keys", json={})
    big = {f"k{i}": ["v" * 200] * 100 for i in range(20)}
    for body in (
        {"caller": {"subject": "u", "attributes": big}},
        {"caller": {"subject": "u"}, "audience": "bad\naudience"},
        {"caller": {"subject": "u"}, "ttl_seconds": True},
        {"caller": {"subject": "u"}, "ttl_seconds": 24 * 3600 + 1},
    ):
        resp = await client.post("/v1/caller-tokens", json=body)
        assert resp.status_code == 422, (body, resp.text)


async def test_active_caller_keys_are_capped(client):
    ids = [(await client.post("/v1/caller-keys", json={})).json()["id"] for _ in range(5)]
    assert (await client.post("/v1/caller-keys", json={})).status_code == 409
    await client.delete(f"/v1/caller-keys/{ids[0]}")
    assert (await client.post("/v1/caller-keys", json={})).status_code == 201


async def test_bad_tokens_are_refused_before_the_run(client):
    await client.post("/v1/caller-keys", json={})
    agent_id = await _agent(client)
    run = f"/v1/agents/{agent_id}/run"

    resp = await client.post(run, json={"input": "x", "caller_token": "x0r.v1.e30.AAAA"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "caller_token_malformed"   # no key id
    unsigned = _sign(key_id="ck_nobody", audience="x0r-core")
    resp = await client.post(run, json={"input": "x", "caller_token": unsigned})
    assert resp.json()["detail"]["reason"] == "caller_signature_invalid"

    wrong_aud = await _token(client, audience="mcp-proxy")
    resp = await client.post(run, json={"input": "x", "caller_token": wrong_aud})
    # Signed with the proxy's audience key, so Core's key doesn't match it.
    assert resp.json()["detail"]["reason"] == "caller_signature_invalid"

    both = {"input": "x", "caller": {"subject": "u"}, "caller_token": wrong_aud}
    assert (await client.post(run, json=both)).status_code == 422
    assert (await client.get("/v1/runs")).json() == []


async def test_revoked_key_stops_its_tokens_and_rotation_works(client):
    old = (await client.post("/v1/caller-keys", json={"name": "old"})).json()
    agent_id = await _agent(client)
    token = await _token(client)
    new = (await client.post("/v1/caller-keys", json={"name": "new"})).json()
    run = f"/v1/agents/{agent_id}/run"

    # A new key only verifies at first: tokens are still signed with the old one, so
    # proxies have time to install the new verifier key.
    assert new["signing"] is False
    minted = (await client.post("/v1/caller-tokens", json={"caller": {"subject": "u"}})).json()
    assert minted["key_id"] == old["id"]
    assert (await client.post(run, json={"input": "x", "caller_token": token})).status_code \
        == 200

    activated = (await client.post(f"/v1/caller-keys/{new['id']}/activate")).json()
    assert activated["signing"] is True
    minted = (await client.post("/v1/caller-tokens", json={"caller": {"subject": "u"}})).json()
    assert minted["key_id"] == new["id"]
    keys = {k["id"]: k["signing"] for k in (await client.get("/v1/caller-keys")).json()}
    assert keys == {old["id"]: False, new["id"]: True}

    assert (await client.delete(f"/v1/caller-keys/{old['id']}")).status_code == 204
    resp = await client.post(run, json={"input": "x", "caller_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "caller_signature_invalid"

    # Revoking the signing key leaves none: minting waits for an activation.
    await client.delete(f"/v1/caller-keys/{new['id']}")
    assert (await client.post("/v1/caller-tokens", json={
        "caller": {"subject": "u"}})).status_code == 409


async def test_key_creation_is_retry_safe(client):
    headers = {"Idempotency-Key": "req-1"}
    first = (await client.post("/v1/caller-keys", json={}, headers=headers)).json()
    again = await client.post("/v1/caller-keys", json={}, headers=headers)
    assert again.status_code == 201 and again.json()["id"] == first["id"]
    assert len((await client.get("/v1/caller-keys")).json()) == 1


async def test_server_secret_is_required_and_kept_out_of_the_database(client, monkeypatch):
    from x0r_core.config import get_settings

    key = (await client.post("/v1/caller-keys", json={})).json()
    token = await _token(client)
    agent_id = await _agent(client)
    monkeypatch.setattr(get_settings(), "caller_key_secret", "another-server-secret-" + "y" * 16)
    # Same database, different server secret: old tokens no longer verify.
    resp = await client.post(f"/v1/agents/{agent_id}/run",
                             json={"input": "x", "caller_token": token})
    assert resp.status_code == 401
    monkeypatch.setattr(get_settings(), "caller_key_secret", "")
    assert (await client.post("/v1/caller-keys", json={})).status_code == 503
    assert (await client.post(f"/v1/caller-keys/{key['id']}/verifier",
                              json={"audience": "p"})).status_code == 503


async def test_oversized_token_and_blank_list_items(client):
    await client.post("/v1/caller-keys", json={})
    agent_id = await _agent(client)
    resp = await client.post(f"/v1/agents/{agent_id}/run",
                             json={"input": "x", "caller_token": "x" * 10_000})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "caller_token_malformed"
    resp = await client.post(f"/v1/agents/{agent_id}/run", json={
        "input": "x", "caller": {"subject": "u", "attributes": {"orders": ["A-1", "  "]}}})
    assert resp.status_code == 422


async def test_keys_and_tokens_stay_in_their_workspace(client, workspace_factory):
    key = (await client.post("/v1/caller-keys", json={})).json()
    token = await _token(client)
    _, other_raw = await workspace_factory("other")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {other_raw}"}) as other:
        assert (await other.delete(f"/v1/caller-keys/{key['id']}")).status_code == 404
        assert (await other.get("/v1/caller-keys")).json() == []
        agent_id = await _agent(other)
        resp = await other.post(f"/v1/agents/{agent_id}/run",
                                json={"input": "x", "caller_token": token})
        assert resp.status_code == 401


async def test_caller_attribute_lists_reach_rules(client):
    agent_id = await _agent(client)
    resp = await client.post(f"/v1/agents/{agent_id}/run", json={
        "input": "warranty?",
        "caller": {"subject": "u", "attributes": {"orders": ["A-1", "A-2"], "role": "x"}},
    })
    assert resp.status_code == 200, resp.text
    bad = await client.post(f"/v1/agents/{agent_id}/run", json={
        "input": "x", "caller": {"subject": "u", "attributes": {"orders": ["ok", "a\nb"]}},
    })
    assert bad.status_code == 422


def test_unicode_payloads_and_bad_key_ids():
    caller = Caller("José", {"city": "Zürich", "tags": ("café",)})
    token = _sign(caller)
    assert _verify(token) == caller
    # Unescaped UTF-8 JSON, signed correctly, is accepted too.
    raw = _resign(token, lambda p: None)
    assert _verify(raw).subject == "José"
    for kid in ("\ud800", "ck 1", "ck/1", "x" * 65):
        bad = _resign(token, lambda p, kid=kid: p.update(kid=kid))
        with pytest.raises(CallerTokenError) as info:
            token_key_id(bad)
        assert info.value.reason == "caller_token_malformed"
    lone = _resign(token, lambda p: p.update(sub="a\ud800"))
    assert _reason(lone) == "caller_token_malformed"
    with pytest.raises(ValueError):
        _sign(key_id="ck 1")


async def test_surrogate_key_id_gets_a_401_not_a_crash(client):
    await client.post("/v1/caller-keys", json={})
    agent_id = await _agent(client)
    body = base64.urlsafe_b64encode(b'{"kid":"\\ud800"}').rstrip(b"=").decode()
    resp = await client.post(f"/v1/agents/{agent_id}/run",
                             json={"input": "x", "caller_token": f"x0r.v1.{body}.AA"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "caller_token_malformed"
