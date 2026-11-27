"""TOML policy validation and shared rule cases."""
from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from test_sdk_wrapper import DictClient, _call, _create, _reply
from x0r_guard import (
    Caller,
    HookContext,
    Policy,
    PolicyFile,
    PolicyFileError,
    load_policy_file,
    load_policy_text,
    wrap,
)

EXAMPLE_POLICY = Path(__file__).resolve().parents[1] / "examples" / "policy.toml"
RULE_POLICY = b"""
version = 1
allow = ["lookup", "refund", "transfer", "ship", "view", "delete"]

[rules.lookup]
caller_owns = { argument = "order", attribute = "orders" }
[rules.refund]
max = { argument = "amount", value = 50 }
[rules.transfer]
min = { argument = "amount", value = 2.5 }
[rules.ship]
one_of = { argument = "method", values = ["ground", "air", 1] }
[rules.view]
caller_has = { attribute = "role", value = "staff" }
[rules.delete]
deny = { reason = "deletion_disabled" }
"""

# Tool, arguments, caller attributes (None means no caller), allowed, reason.
POLICY_CASES = [
    ("lookup", {"order": "A-1"}, {"orders": ("A-1", "A-2")}, True, "granted"),
    ("lookup", {"order": "A-1"}, {"orders": "A-1"}, True, "granted"),
    ("lookup", {"order": "A-1"}, {"orders": "A-1,A-2"}, False, "rule_caller_owns"),
    ("lookup", {"order": "A-1"}, {"orders": ("A-10",)}, False, "rule_caller_owns"),
    ("lookup", {"order": "A-1"}, {}, False, "rule_caller_owns"),
    ("lookup", {"order": "A-1"}, None, False, "rule_caller_owns"),
    ("lookup", {}, {"orders": ""}, False, "rule_caller_owns"),
    ("lookup", {"order": 1}, {"orders": ("1",)}, False, "rule_caller_owns"),
    ("refund", {"amount": 50}, None, True, "granted"),
    ("refund", {"amount": 50.1}, None, False, "rule_max"),
    ("refund", {"amount": True}, None, False, "rule_max"),
    ("refund", {"amount": "50"}, None, False, "rule_max"),
    ("refund", {}, None, False, "rule_max"),
    ("refund", {"amount": float("nan")}, None, False, "rule_max"),
    ("transfer", {"amount": 2.5}, None, True, "granted"),
    ("transfer", {"amount": 2}, None, False, "rule_min"),
    ("transfer", {"amount": False}, None, False, "rule_min"),
    ("transfer", {"amount": "3"}, None, False, "rule_min"),
    ("transfer", {}, None, False, "rule_min"),
    ("transfer", {"amount": float("inf")}, None, False, "rule_min"),
    ("ship", {"method": "ground"}, None, True, "granted"),
    ("ship", {"method": 1.0}, None, True, "granted"),
    ("ship", {"method": "sea"}, None, False, "rule_one_of"),
    ("ship", {"method": True}, None, False, "rule_one_of"),
    ("ship", {}, None, False, "rule_one_of"),
    ("view", {}, {"role": "staff"}, True, "granted"),
    ("view", {}, {"role": "staffing"}, False, "rule_caller_has"),
    ("view", {}, {"role": ("staff",)}, False, "rule_caller_has"),
    ("view", {}, {}, False, "rule_caller_has"),
    ("view", {}, None, False, "rule_caller_has"),
    ("delete", {}, None, False, "deletion_disabled"),
]


@pytest.mark.parametrize("tool,arguments,attrs,allowed,reason", POLICY_CASES)
def test_rule_cases(tool, arguments, attrs, allowed, reason):
    policy = load_policy_text(RULE_POLICY)
    caller = Caller("user", attrs) if attrs is not None else None
    result = policy.rules[tool](arguments, HookContext(caller, None, None))
    assert (result is None, "granted" if result is None else result) == (allowed, reason)


@pytest.mark.parametrize("suffix,key", [
    ('other = true', "other"),
    ('[rules.other]\ndeny = { reason = "blocked" }', "rules.other"),
    ('[rules.""]\ndeny = { reason = "blocked" }', "rules."),
    ('rules = []', "rules"),
    ('rules.lookup = true', "rules.lookup"),
    ('[rules.lookup]', "rules.lookup"),
    ('[rules.lookup]\nunknown = {}', "rules.lookup.unknown"),
    ('[rules.lookup]\nmax = 50', "rules.lookup.max"),
    ('[rules.lookup]\nmax = { argument = "x", value = 5, typo = true }', "max.typo"),
    ('[rules.lookup]\nmax = { argument = "x" }', "max.value"),
    ('[rules.lookup]\nmax = { argument = "x", value = true }', "max.value"),
    ('[rules.lookup]\nmin = { argument = "x", value = "5" }', "min.value"),
    ('[rules.lookup]\nmin = { argument = "x", value = nan }', "min.value"),
    ('[rules.lookup]\none_of = { argument = "x", values = "x" }', "one_of.values"),
    ('[rules.lookup]\none_of = { argument = "x", values = [{}] }', "one_of.values"),
    ('[rules.lookup]\ncaller_owns = { argument = 1, attribute = "a" }', "argument"),
    ('[rules.lookup]\ncaller_has = { attribute = false, value = "a" }', "attribute"),
    ('[rules.lookup]\ncaller_has = { attribute = "a", value = ["x"] }', "caller_has.value"),
    ('[rules.lookup]\ndeny = { reason = 1 }', "deny.reason"),
    ('[rules.lookup]\ndeny = { reason = "" }', "deny.reason"),
    ('[rules.lookup', "invalid TOML"),
])
def test_invalid_rules_and_keys(tmp_path, suffix, key):
    path = tmp_path / "policy.toml"
    path.write_text(f'version = 1\nallow = ["lookup"]\n{suffix}\n', encoding="utf-8")
    with pytest.raises(PolicyFileError) as error:
        load_policy_file(path)
    assert str(path) in str(error.value)
    assert key in str(error.value)


@pytest.mark.parametrize("data,key", [
    (b'allow = []', "version"),
    (b'version = 2\nallow = []', "version"),
    (b'version = true\nallow = []', "version"),
    (b'version = 1.0\nallow = []', "version"),
    (b'version = 1', "allow"),
    (b'version = 1\nallow = "lookup"', "allow"),
    (b'version = 1\nallow = [1]', "allow[0]"),
    (b'version = 1\nallow = [""]', "allow[0]"),
    (b'\xff', "invalid TOML"),
])
def test_invalid_policy_text(data, key):
    with pytest.raises(PolicyFileError) as error:
        load_policy_text(data, source="candidate")
    assert "candidate" in str(error.value)
    assert key in str(error.value)


def test_unreadable_policy(tmp_path):
    path = tmp_path / "missing.toml"
    with pytest.raises(PolicyFileError, match="missing.toml"):
        load_policy_file(path)


def test_example_policy_and_digest():
    data = EXAMPLE_POLICY.read_bytes()
    policy = load_policy_file(EXAMPLE_POLICY)
    assert isinstance(policy, PolicyFile)
    assert policy.version == 1
    assert policy.allow == frozenset({"lookup_order", "refund_order"})
    assert policy.digest == load_policy_text(data).digest == hashlib.sha256(data).hexdigest()
    assert load_policy_text(data + b"\n").digest != policy.digest
    with pytest.raises(FrozenInstanceError):
        policy.version = 2
    with pytest.raises(TypeError):
        policy.rules["lookup_order"] = policy.rules["refund_order"]


def test_example_policy_with_wrap():
    records = []
    policy = Policy.from_file(EXAMPLE_POLICY, on_decision=records.append, on_denied="remove")
    inner = DictClient(_reply(
        _call("lookup_order", {"order_id": "A-1"}, "lookup"),
        _call("refund_order", {"order_id": "A-1", "amount": 50}, "allowed"),
        _call("refund_order", {"order_id": "A-1", "amount": 51}, "too_large"),
        _call("refund_order", {"order_id": "A-2", "amount": 51}, "not_owned"),
    ))
    reply = _create(wrap(inner, policy, caller=Caller("user", {"orders": ("A-1",)})))
    calls = reply["choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["lookup", "allowed"]
    assert [(r.allowed, r.reason) for r in records] == [
        (True, "granted"), (True, "granted"), (False, "rule_max"), (False, "rule_caller_owns"),
    ]
    assert {r.policy_digest for r in records} == {load_policy_file(EXAMPLE_POLICY).digest}
    assert policy.policy_digest == records[0].policy_digest


def test_from_file_adds_python_rules_without_replacing_file_rules(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_bytes(RULE_POLICY.replace(b'"lookup",', b'"python_tool", "lookup",'))
    config = object()

    def python_rule(arguments, ctx):
        assert ctx.config is config
        return "python_denial"

    policy = Policy.from_file(path, rules={"python_tool": python_rule}, config=config)
    ctx = HookContext(None, None, policy.config)
    assert policy.rules["python_tool"]({}, ctx) == "python_denial"
    assert "lookup" in policy.rules
    with pytest.raises(PolicyFileError, match=r"policy\.toml: rules\.lookup"):
        Policy.from_file(path, rules={"lookup": python_rule})


def test_python_policy_decision_has_no_digest():
    records = []
    inner = DictClient(_reply(_call("lookup_order", {"order_id": "A-1"})))
    _create(wrap(inner, Policy(allow=["lookup_order"], on_decision=records.append)))
    assert records[0].policy_digest is None
