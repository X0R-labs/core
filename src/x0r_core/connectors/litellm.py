"""LiteLLM proxy guardrail for non-streaming chat completions."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from litellm import ModelResponse
from litellm.integrations.custom_guardrail import CustomGuardrail
from x0r_guard.identity import CallerTokenError, token_key_id, verify_caller
from x0r_guard.sdk import (
    DecisionNotRecorded,
    DecisionRecord,
    Policy,
    ToolCallDenied,
    UnguardedCall,
    _offered_tools,
    check_reply,
)
from x0r_guard.sink import core_sink

from .mcp import caller_key as decode_caller_key

_SNAPSHOT = "x0r_guard_tools"


# Checking and saving every decision for one reply must finish in this many seconds,
# or the reply is withheld.
CHECK_TIMEOUT = 30

# Call types that never return tool calls, so they pass through unchecked.
_NO_TOOL_CALLS = frozenset({
    "embedding", "aembedding", "moderation", "amoderation",
    "image_generation", "aimage_generation", "image_edit", "aimage_edit",
    "transcription", "atranscription",
    "speech", "aspeech",
})


class X0RGuardrail(CustomGuardrail):
    """Register for both pre_call and post_call, with default_on enabled."""

    use_native_lifecycle_hooks = True

    def __init__(
        self, *, policy_file: str | None = None, on_denied: str | None = None,
        caller_key: str | None = None, audience: str | None = None,
        decision_log: str | None = None, core_url: str | None = None,
        api_key: str | None = None, **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        policy_file = policy_file or os.getenv("X0R_POLICY_FILE")
        if not policy_file:
            raise ValueError("X0R_POLICY_FILE is required for the LiteLLM guardrail")
        self.policy = Policy.from_file(
            policy_file, on_decision=self._save,
            on_denied=os.getenv("X0R_ON_DENIED", "raise") if on_denied is None else on_denied,
        )
        key = caller_key or os.getenv("X0R_CALLER_KEY")
        self.caller_key = decode_caller_key(key) if key else None
        self.audience = audience or os.getenv("X0R_CALLER_AUDIENCE", "litellm")
        log = decision_log or os.getenv("X0R_DECISION_LOG")
        self.decision_log = Path(log) if log else None
        url = core_url or os.getenv("X0R_CORE_URL")
        key = api_key or os.getenv("X0R_API_KEY")
        if url and not key:
            raise ValueError("X0R_CORE_URL requires X0R_API_KEY for decision logging")
        self.core = core_sink(url, key, source="litellm") if url and key else None
        if self.decision_log is None and self.core is None:
            raise ValueError("Set X0R_DECISION_LOG or X0R_CORE_URL for decision logging")

    def _save(self, record: DecisionRecord) -> None:
        if self.decision_log is not None:
            with self.decision_log.open("a", encoding="utf-8") as file:
                file.write(json.dumps({**asdict(record), "source": "litellm"}) + "\n")
        if self.core is not None:
            self.core(record)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type in _NO_TOOL_CALLS:
            return None
        if call_type not in {"completion", "acompletion"}:
            # Other endpoints (responses, messages, text completion, ...) can return tool
            # calls this guardrail doesn't check yet, so they're refused.
            raise HTTPException(400, f"X0R guardrail doesn't check {call_type} calls yet")
        if data.get("stream"):
            raise HTTPException(400, "X0R guardrail requires stream=false")
        if data.get("functions") or data.get("function_call"):
            raise HTTPException(400, "X0R guardrail requires tools= for function calls")
        extra_body = data.get("extra_body")
        if isinstance(extra_body, dict) and (
            extra_body.keys() & {"stream", "tools", "tool_choice", "functions", "function_call"}
        ):
            raise HTTPException(400, "Pass tool and streaming options outside extra_body")

        tools = copy.deepcopy(data.get("tools") or [])
        try:
            _offered_tools(tools, self.policy.rules)
        except UnguardedCall as exc:
            raise HTTPException(400, str(exc)) from None
        # LiteLLM carries proxy_server_request through the model call as request metadata.
        data.setdefault("proxy_server_request", {})[_SNAPSHOT] = tools
        return data

    def runs_post_call_deployment_hook(self, request_data):
        # Check once at the proxy boundary, after deployment retries and fallbacks.
        return False

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        request = data.get("proxy_server_request") or {}
        if _SNAPSHOT not in request:
            # Only calls that can't return tool calls skip the pre-call snapshot.
            if isinstance(response, ModelResponse):
                raise HTTPException(500, "X0R guardrail is missing its pre-call tool snapshot")
            return response
        if not isinstance(response, ModelResponse):
            raise HTTPException(500, "X0R guardrail can't check this reply type; it was withheld")
        offered = _offered_tools(request[_SNAPSHOT], self.policy.rules)
        headers = request.get("headers") or {}
        caller = None
        caller_error = None
        for name, token in headers.items():
            if name.lower() == "x-x0r-caller":
                try:
                    keys = {token_key_id(token): self.caller_key} if self.caller_key else {}
                    caller = verify_caller(token, keys=keys, audience=self.audience)
                except CallerTokenError as exc:
                    caller_error = exc.reason
                break
        try:
            return await asyncio.wait_for(asyncio.to_thread(
                check_reply, response, caller, offered, self.policy, caller_error=caller_error,
            ), CHECK_TIMEOUT)
        except (ToolCallDenied, UnguardedCall) as exc:
            raise HTTPException(400, str(exc)) from None
        except (DecisionNotRecorded, TimeoutError):
            raise HTTPException(
                503, "X0R could not record decisions. The reply was withheld.",
            ) from None
