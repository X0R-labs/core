"""Load TOML policies and compile their rules into guard hooks."""
from __future__ import annotations

import hashlib
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .engine import Authorizer, HookContext, _same


class PolicyFileError(ValueError):
    """A policy file could not be read or validated."""


@dataclass(frozen=True)
class PolicyFile:
    allow: frozenset[str]
    rules: Mapping[str, Authorizer]
    digest: str
    version: int


_RULE_KEYS = {
    "caller_owns": {"argument", "attribute"},
    "max": {"argument", "value"},
    "min": {"argument", "value"},
    "one_of": {"argument", "values"},
    "caller_has": {"attribute", "value"},
    "deny": {"reason"},
}


def _number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _check_keys(
    table: dict, keys: set[str], source: str, key: str = "",
) -> None:
    unknown = table.keys() - keys
    if unknown:
        raise PolicyFileError(f"{source}: {key}{min(unknown)} is an unknown key")


def _compile_rule(kind: str, spec: Any, source: str, key: str) -> Authorizer:
    if kind not in _RULE_KEYS:
        raise PolicyFileError(f"{source}: {key} is an unknown rule type")
    if not isinstance(spec, dict):
        raise PolicyFileError(f"{source}: {key} must be a table")
    _check_keys(spec, _RULE_KEYS[kind], source, f"{key}.")
    for name in sorted(_RULE_KEYS[kind]):
        value = spec.get(name)
        if name == "values":
            valid = isinstance(value, list) and all(
                isinstance(item, (str, bool)) or _number(item) for item in value
            )
            expected = "an array of strings, booleans or finite numbers"
        elif name == "value" and kind in {"max", "min"}:
            valid = _number(value)
            expected = "a finite number"
        elif name == "value":
            valid = isinstance(value, str)
            expected = "a string"
        else:
            valid = isinstance(value, str) and bool(value.strip())
            expected = "a non-empty string"
        if not valid:
            raise PolicyFileError(f"{source}: {key}.{name} must be {expected}")

    argument = spec.get("argument")
    attribute = spec.get("attribute")
    value = spec.get("value")
    values = tuple(spec.get("values", ()))
    reason = spec.get("reason", f"rule_{kind}")

    def authorize(arguments: dict[str, Any], ctx: HookContext) -> str | None:
        actual = arguments.get(argument)
        if kind in {"caller_owns", "caller_has"}:
            attr = ctx.caller.attributes.get(attribute) if ctx.caller is not None else None
            if kind == "caller_has":
                passed = isinstance(attr, str) and attr == value
            else:
                owned = (attr,) if isinstance(attr, str) else attr
                passed = isinstance(actual, str) and isinstance(owned, tuple) and actual in owned
        elif kind == "max":
            passed = _number(actual) and actual <= value
        elif kind == "min":
            passed = _number(actual) and actual >= value
        elif kind == "one_of":
            passed = any(_same(actual, option) for option in values)
        else:
            passed = False
        return None if passed else reason

    return authorize


def _all_rules(rules: tuple[Authorizer, ...]) -> Authorizer:
    def authorize(arguments: dict[str, Any], ctx: HookContext) -> str | None:
        for rule in rules:
            reason = rule(arguments, ctx)
            if reason is not None:
                return reason
        return None

    return authorize


def load_policy_text(data: bytes, *, source: str = "<string>") -> PolicyFile:
    """Validate UTF-8 TOML and hash the exact bytes supplied."""
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise PolicyFileError(f"{source}: invalid TOML: {exc}") from exc

    _check_keys(document, {"version", "allow", "rules"}, source)
    version = document.get("version")
    if type(version) is not int or version != 1:
        raise PolicyFileError(f"{source}: version must be the integer 1")
    allowed = document.get("allow")
    if not isinstance(allowed, list):
        raise PolicyFileError(f"{source}: allow must be an array of tool names")
    for index, tool in enumerate(allowed):
        if not isinstance(tool, str) or not tool.strip():
            raise PolicyFileError(f"{source}: allow[{index}] must be a non-empty tool name")
    allow = frozenset(allowed)

    tables = document.get("rules", {})
    if not isinstance(tables, dict):
        raise PolicyFileError(f"{source}: rules must be a table")
    rules = {}
    for tool, table in tables.items():
        key = f"rules.{tool}" if tool.strip() else f"rules.{tool!r}"
        if not tool.strip():
            raise PolicyFileError(f"{source}: {key} must name a non-empty tool")
        if tool not in allow:
            raise PolicyFileError(f"{source}: {key} names a tool missing from allow")
        if not isinstance(table, dict) or not table:
            raise PolicyFileError(f"{source}: {key} must be a non-empty table")
        rules[tool] = _all_rules(tuple(
            _compile_rule(kind, spec, source, f"{key}.{kind}")
            for kind, spec in table.items()
        ))
    return PolicyFile(allow, MappingProxyType(rules), hashlib.sha256(data).hexdigest(), version)


def load_policy_file(path: str | Path) -> PolicyFile:
    """Read and validate a policy file."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise PolicyFileError(f"{path}: cannot read policy file: {exc}") from exc
    return load_policy_text(data, source=str(path))
