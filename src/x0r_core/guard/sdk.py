"""Guard any OpenAI-compatible client with one line.

    from openai import OpenAI
    from x0r_core.guard import Caller, Policy, wrap

    client = wrap(OpenAI(), Policy(allow=["lookup_order"]))
    reply = client.chat.completions.create(model=..., messages=..., tools=TOOLS,
                                           x0r_caller=Caller("user-42"))

Every tool call in the reply is checked by the guard before your code sees it. Each
decision is handed to ``Policy.on_decision`` first (save it there). Denied calls either
raise ``ToolCallDenied`` or are removed from the reply, as the policy says.

This module uses only the standard library and never imports ``openai``: it works with
any client that has the same shape (OpenAI, AsyncOpenAI, Azure, OpenRouter, vLLM, ...).
Paths it can't check yet (streaming, the Responses API, beta endpoints, raw HTTP) are
refused instead of passed through unchecked.

The wrapper guards the public client API. Private attributes (``_client``, ``_inner``)
reach the unwrapped client and are outside what it promises.
"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal

from .engine import POLICY_VERSION, ActionRequest, Authorizer, Caller, Decision, check_action

_SCOPE = "sdk"
# Client attributes that can't return tool calls. Everything else (responses, beta,
# post(), request(), with_raw_response, ...) is refused: an allowlist, so a new SDK
# path is closed until someone checks it.
_PASSTHROUGH_ATTRS = frozenset({
    "models", "embeddings", "files", "images", "audio", "moderations", "fine_tuning",
    "batches", "uploads", "vector_stores", "containers",
    "api_key", "organization", "project", "base_url", "timeout", "max_retries",
    "default_headers", "default_query", "close",
})
# Request keys that change what comes back. Refused when hidden in extra_body.
_GUARDED_BODY_KEYS = frozenset({"tools", "tool_choice", "functions", "function_call",
                                "stream", "stream_options", "n"})
_RAW_RESPONSE_HEADER = "x-stainless-raw-response"


@dataclass(frozen=True)
class DecisionRecord:
    """One guard decision, safe to store: it never contains argument values."""

    tool: str
    tool_call_id: str | None
    allowed: bool
    reason: str
    caller_subject: str | None
    policy_version: str
    decided_at: str


@dataclass(frozen=True)
class Policy:
    """What the wrapper enforces.

    allow        tools the operator permits (guard check 3).
    rules        optional per-tool business rules, ``rule(arguments, ctx) -> reason | None``.
    on_decision  called with every DecisionRecord before the reply is returned. Store it
                 here. If it raises, nothing is returned (``DecisionNotRecorded``).
    on_denied    "raise" (default) raises ``ToolCallDenied``; "remove" drops denied tool
                 calls from the reply and returns the rest.
    config       passed to rules as ``ctx.config``.

    The policy is frozen: ``allow`` and ``rules`` are copied when it is made, so changing
    the list you passed in later does not change what is enforced.
    """

    allow: Collection[str]
    rules: Mapping[str, Authorizer] = field(default_factory=dict)
    on_decision: Callable[[DecisionRecord], None] | None = None
    on_denied: Literal["raise", "remove"] = "raise"
    config: Any = None

    def __post_init__(self) -> None:
        if isinstance(self.allow, (str, bytes)):
            raise TypeError("Policy.allow must be a list of tool names, not a string")
        if self.on_denied not in ("raise", "remove"):
            raise ValueError('Policy.on_denied must be "raise" or "remove"')
        if inspect.iscoroutinefunction(self.on_decision):
            raise TypeError("Policy.on_decision must be a plain function, not async")
        object.__setattr__(self, "allow", frozenset(self.allow))
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))


class GuardError(Exception):
    """Base class for errors raised by the wrapper."""


class ToolCallDenied(GuardError):
    """The model asked for at least one tool call the guard denied.

    ``response`` is the reply with the denied calls already removed, so nothing in this
    exception can be run by mistake.
    """

    def __init__(self, denied: list[DecisionRecord], decisions: list[DecisionRecord],
                 response: Any) -> None:
        names = ", ".join(f"{d.tool} ({d.reason})" for d in denied)
        super().__init__(f"Tool call denied: {names}")
        self.denied = denied
        self.decisions = decisions
        self.response = response


class DecisionNotRecorded(GuardError):
    """``on_decision`` failed, so the reply was withheld and no tool call is released."""


class UnguardedCall(GuardError):
    """The call would bypass the guard (streaming, Responses API, ...), so it was refused."""


class UnguardedAttribute(UnguardedCall, AttributeError):
    """A client attribute that would bypass the guard. ``hasattr()`` reports it missing."""


def _refuse(path: str, name: str, hint: str) -> UnguardedAttribute | AttributeError:
    if name.startswith("__"):
        return AttributeError(name)
    return UnguardedAttribute(f"{path}{name} isn't guarded yet; {hint}")


# ── helpers that read both SDK objects and plain dicts ───────────────────────

def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _set(obj: Any, name: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


@dataclass(frozen=True)
class _OfferedTool:
    name: str
    parameters: dict[str, Any]
    authorize: Authorizer | None


def _offered_tools(tools: Any, rules: Mapping[str, Authorizer]) -> dict[str, _OfferedTool]:
    offered: dict[str, _OfferedTool] = {}
    for spec in tools or []:
        if _get(spec, "type", "function") != "function":
            continue
        fn = _get(spec, "function") or {}
        name = _get(fn, "name")
        if not isinstance(name, str) or not name:
            continue
        if name in offered:
            raise UnguardedCall(f"tool {name!r} is offered twice; offer each tool once")
        parameters = _get(fn, "parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        offered[name] = _OfferedTool(name, parameters, rules.get(name))
    return offered


def _parse_arguments(raw: Any) -> object:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return raw  # not an object: the guard denies it as invalid_arguments
    if raw == "":
        return {}  # some providers send "" for a tool without parameters
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _dump(parsed: Any) -> Any:
    if hasattr(parsed, "model_dump"):
        return parsed.model_dump(mode="json", exclude_unset=True)
    return parsed


# ── the checking itself ──────────────────────────────────────────────────────

class _Checker:
    def __init__(self, policy: Policy, default_caller: Caller | None) -> None:
        self.policy = policy
        self.default_caller = default_caller

    def before(self, kwargs: dict[str, Any]) -> tuple[dict[str, Any], Caller | None, dict]:
        caller = kwargs.pop("x0r_caller", self.default_caller)
        if caller is not None and not isinstance(caller, Caller):
            raise TypeError("x0r_caller must be a Caller")
        if caller is not None:
            # Rules see the caller as it was when the request started.
            caller = dataclasses.replace(caller, attributes=MappingProxyType(
                dict(caller.attributes)))
        if kwargs.get("stream"):
            raise UnguardedCall("stream=True isn't guarded yet; call without streaming")
        if kwargs.get("functions") or kwargs.get("function_call"):
            raise UnguardedCall("the legacy functions= API isn't guarded; use tools=")
        extra_body = kwargs.get("extra_body")
        if isinstance(extra_body, Mapping) and _GUARDED_BODY_KEYS & extra_body.keys():
            hidden = ", ".join(sorted(_GUARDED_BODY_KEYS & extra_body.keys()))
            raise UnguardedCall(f"extra_body may not set {hidden}; pass them as arguments")
        if extra_body is not None and not isinstance(extra_body, Mapping):
            raise UnguardedCall("extra_body must be a dict")
        headers = kwargs.get("extra_headers") or {}
        if any(str(k).lower() == _RAW_RESPONSE_HEADER for k in headers):
            raise UnguardedCall("raw responses aren't guarded; drop the raw-response header")
        if kwargs.get("tools") is not None:
            # One copy is both sent and checked, so changing your list (or passing a
            # generator) can't make the two differ.
            kwargs["tools"] = copy.deepcopy(list(kwargs["tools"]))
        offered = _offered_tools(kwargs.get("tools"), self.policy.rules)
        return kwargs, caller, offered

    def after(self, response: Any, caller: Caller | None, offered: dict) -> Any:
        choices = _get(response, "choices")
        if not isinstance(choices, (list, tuple)):
            # A raw HTTP response or anything else that isn't a completion: we can't see
            # its tool calls, so it never reaches the app.
            raise UnguardedCall("the reply isn't a chat completion, so it can't be checked")

        decisions: list[DecisionRecord] = []
        edits: list[tuple[Any, Any, list[Any], bool, bool]] = []
        seen_ids: set[str] = set()
        for choice in choices:
            message = _get(choice, "message")
            if message is None:
                continue
            fc = _get(message, "function_call")
            drop_fc = False
            if fc:
                # Legacy single function call: never produced when tools= is used.
                decisions.append(self._record(_get(fc, "name") or "", None,
                                              Decision(False, "unsupported_tool_type"),
                                              caller))
                drop_fc = True
            calls = list(_get(message, "tool_calls") or [])
            kept = []
            for call in calls:
                decision = self._decide(call, caller, offered, seen_ids)
                fn = _get(call, "function") or {}
                decisions.append(self._record(_get(fn, "name") or _get(call, "type") or "",
                                              _get(call, "id"), decision, caller))
                if decision.allowed:
                    kept.append(call)
            if drop_fc or len(kept) != len(calls):
                edits.append((choice, message, kept, drop_fc, bool(calls)))

        self._save(decisions)
        if not edits:
            return response
        # Remove what was denied, from the same objects that were checked.
        for choice, message, kept, drop_fc, had_calls in edits:
            if drop_fc:
                _set(message, "function_call", None)
            if had_calls:
                _set(message, "tool_calls", kept or None)
            if not kept and _get(choice, "finish_reason") in ("tool_calls", "function_call"):
                _set(choice, "finish_reason", "stop")
        if self.policy.on_denied == "raise":
            raise ToolCallDenied([d for d in decisions if not d.allowed], decisions, response)
        return response

    def _save(self, decisions: list[DecisionRecord]) -> None:
        if self.policy.on_decision is None:
            return
        for record in decisions:
            try:
                result = self.policy.on_decision(record)
            except Exception as exc:
                raise DecisionNotRecorded(
                    "on_decision failed, so the reply was withheld and no tool call ran"
                ) from exc
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise DecisionNotRecorded("on_decision returned an awaitable; it must "
                                          "save the record before it returns")

    def _decide(self, call: Any, caller: Caller | None, offered: dict,
                seen_ids: set[str]) -> Decision:
        if _get(call, "type", "function") != "function":
            return Decision(False, "unsupported_tool_type")
        call_id = _get(call, "id")
        if not isinstance(call_id, str) or not call_id:
            return Decision(False, "missing_tool_call_id")
        if call_id in seen_ids:
            return Decision(False, "duplicate_tool_call_id")
        seen_ids.add(call_id)
        fn = _get(call, "function") or {}
        name = _get(fn, "name")
        if not isinstance(name, str):
            return Decision(False, "unknown_tool")
        arguments = _parse_arguments(_get(fn, "arguments"))
        parsed = _get(fn, "parsed_arguments")
        if parsed is not None and _dump(parsed) != arguments:
            # parse() built parsed_arguments with the app's models, and a validator changed
            # a value. The app would run something other than what was checked.
            return Decision(False, "parsed_arguments_changed")
        request = ActionRequest(
            workspace_id=_SCOPE,
            agent_workspace_id=_SCOPE,
            agent_tools=list(offered),
            tool_name=name,
            arguments=arguments,
            caller=caller,
            tool_call_id=call_id,
        )
        return check_action(
            request, operator_tools=list(self.policy.allow), tools=offered,
            config=self.policy.config,
        )

    def _record(self, tool: str, call_id: Any, decision: Decision,
                caller: Caller | None) -> DecisionRecord:
        return DecisionRecord(
            tool=str(tool)[:120],
            tool_call_id=call_id if isinstance(call_id, str) else None,
            allowed=decision.allowed,
            reason=decision.reason,
            caller_subject=caller.subject if caller else None,
            policy_version=POLICY_VERSION,
            decided_at=datetime.now(UTC).isoformat(),
        )


# ── the wrapper objects ──────────────────────────────────────────────────────

class _GuardedCompletions:
    def __init__(self, inner: Any, checker: _Checker) -> None:
        self._inner = inner
        self._checker = checker

    def create(self, **kwargs: Any) -> Any:
        return self._call(self._inner.create, kwargs)

    def parse(self, **kwargs: Any) -> Any:
        return self._call(self._inner.parse, kwargs)

    def _call(self, method: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        kwargs, caller, offered = self._checker.before(dict(kwargs))
        try:
            result = method(**kwargs)
        except Exception as exc:
            self._check_attached(exc, caller, offered)
            raise
        if inspect.isawaitable(result):
            async def finish() -> Any:
                try:
                    done = await result
                except Exception as exc:
                    self._check_attached(exc, caller, offered)
                    raise
                return self._checker.after(done, caller, offered)
            return finish()
        return self._checker.after(result, caller, offered)

    def _check_attached(self, exc: Exception, caller: Caller | None, offered: dict) -> None:
        """Some SDK errors carry the completion (parse() on a cut-off reply). Check it
        too, so its tool calls don't reach the app through the exception."""
        completion = getattr(exc, "completion", None)
        if completion is not None:
            self._checker.after(completion, caller, offered)

    def __getattr__(self, name: str) -> Any:
        raise _refuse("chat.completions.", name, "use create() or parse()")


class _GuardedChat:
    def __init__(self, inner: Any, checker: _Checker) -> None:
        self.completions = _GuardedCompletions(inner.completions, checker)

    def __getattr__(self, name: str) -> Any:
        raise _refuse("chat.", name, "use chat.completions")


class GuardedClient:
    """Stands in for the wrapped client. Paths that can't return tool calls pass through
    unchanged (``models``, ``embeddings``, ``files``, ...); every other path is refused."""

    def __init__(self, client: Any, policy: Policy, caller: Caller | None = None) -> None:
        headers = getattr(client, "default_headers", None) or {}
        if any(str(k).lower() == _RAW_RESPONSE_HEADER for k in headers):
            raise UnguardedCall("the client sends the raw-response header by default, so its "
                                "replies can't be checked; remove it from default_headers")
        self._client = client
        self._checker = _Checker(policy, caller)
        self.chat = _GuardedChat(client.chat, self._checker)

    def with_options(self, **kwargs: Any) -> GuardedClient:
        """Same as the client's ``with_options``, still guarded."""
        return self._clone(self._client.with_options, kwargs)

    def copy(self, **kwargs: Any) -> GuardedClient:
        """Same as the client's ``copy``, still guarded."""
        return self._clone(self._client.copy, kwargs)

    def _clone(self, method: Callable[..., Any], kwargs: dict[str, Any]) -> GuardedClient:
        return GuardedClient(method(**kwargs), self._checker.policy,
                             self._checker.default_caller)

    def __enter__(self) -> GuardedClient:
        self._client.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._client.__exit__(*exc)

    async def __aenter__(self) -> GuardedClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._client.__aexit__(*exc)

    def __getattr__(self, name: str) -> Any:
        if name not in _PASSTHROUGH_ATTRS:
            raise _refuse("client.", name, "use chat.completions")
        return getattr(self._client, name)


def wrap(client: Any, policy: Policy, *, caller: Caller | None = None) -> GuardedClient:
    """Return ``client`` with every tool call checked by the guard.

    ``caller`` is the default caller for all requests; pass ``x0r_caller=`` to a single
    ``create()`` call to override it.
    """
    if not isinstance(policy, Policy):
        raise TypeError("wrap() needs a Policy")
    return GuardedClient(client, policy, caller)
