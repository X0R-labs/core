from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── workspace / keys ─────────────────────────────────────────────────────────

class WorkspaceOut(ORMModel):
    id: str
    name: str
    plan: str
    monthly_token_quota: int
    created_at: datetime


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    plan: str = "trial"
    monthly_token_quota: int = Field(default=1_000_000, ge=0)
    key_name: str = "default"


class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: datetime


class WorkspaceCreated(BaseModel):
    workspace: WorkspaceOut
    api_key: str = Field(description="Shown once. Store it now.")
    key: ApiKeyOut


# ── documents ────────────────────────────────────────────────────────────────

class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=1_000_000)
    source: str = "api"
    mime: str = "text/plain"
    meta: dict[str, Any] = Field(default_factory=dict)
    embed: bool = True


class DocumentOut(ORMModel):
    id: str
    title: str
    source: str
    mime: str
    meta: dict = Field(default_factory=dict)
    created_at: datetime
    chunk_count: int = 0


class IngestOut(BaseModel):
    document: DocumentOut
    chunks: int


# ── retrieval ────────────────────────────────────────────────────────────────

class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    k: int = Field(default=5, ge=1, le=20)


class SearchOut(BaseModel):
    query: str
    results: list[dict]
    count: int


# ── agents / runs ────────────────────────────────────────────────────────────

class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    system_prompt: str = Field(default="You are a helpful assistant.", max_length=32_000)
    provider: str | None = None
    model: str | None = None
    tools: list[str] = Field(default_factory=lambda: ["knowledge_search"])
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_steps: int = Field(default=6, ge=1, le=20)


class AgentOut(ORMModel):
    id: str
    name: str
    description: str
    system_prompt: str
    provider: str | None
    model: str | None
    tools: list
    temperature: float
    max_steps: int
    created_at: datetime


class CallerIn(BaseModel):
    """Who the run acts for, as asserted by the app holding the API key.

    An attribute value is a string or a list of strings, e.g. ``{"orders": ["A-1"]}``.
    """

    subject: str = Field(min_length=1, max_length=200, pattern=r"\S")
    attributes: dict[
        Annotated[str, Field(min_length=1, max_length=64)],
        Annotated[str, Field(max_length=200)]
        | Annotated[list[Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]],
                    Field(max_length=100)],
    ] = Field(
        default_factory=dict, max_length=20
    )

    @model_validator(mode="after")
    def _no_control_characters(self) -> CallerIn:
        values = [self.subject, *self.attributes]
        for value in self.attributes.values():
            values.extend(value if isinstance(value, list) else [value])
        if any(ord(ch) < 32 or ord(ch) == 127 for value in values for ch in value):
            raise ValueError("caller fields must not contain control characters")
        return self

    def to_caller(self):
        from .guard import Caller

        return Caller(self.subject, {
            k: tuple(v) if isinstance(v, list) else v for k, v in self.attributes.items()
        })


class RunIn(BaseModel):
    input: str = Field(min_length=1, max_length=32_000)
    caller: CallerIn | None = None
    # No length limit here: an oversized token reaches the verifier and gets its 401 reason.
    caller_token: str | None = None

    @model_validator(mode="after")
    def _one_caller(self) -> RunIn:
        if self.caller is not None and self.caller_token is not None:
            raise ValueError("send caller or caller_token, not both")
        return self


CORE_AUDIENCE = "x0r-core"


class CallerKeyIn(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=120)


class CallerKeyOut(ORMModel):
    id: str
    name: str
    signing: bool
    created_at: datetime
    revoked_at: datetime | None


_AUDIENCE = Field(default=CORE_AUDIENCE, min_length=1, max_length=200, pattern=r"^[\x21-\x7e]+$")


class VerifierKeyIn(BaseModel):
    audience: str = _AUDIENCE


class VerifierKeyOut(BaseModel):
    key_id: str
    audience: str
    key: str = Field(
        description="base64url. Checks (and could sign) tokens for this audience only."
    )


class CallerTokenIn(BaseModel):
    caller: CallerIn
    audience: str = _AUDIENCE
    ttl_seconds: StrictInt = Field(default=300, ge=1, le=24 * 3600)


class CallerTokenOut(BaseModel):
    token: str
    key_id: str
    audience: str
    expires_at: datetime


class RunOut(ORMModel):
    id: str
    agent_id: str | None
    status: str
    input: str
    output: str
    steps: list
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    created_at: datetime


class PolicyDecisionOut(ORMModel):
    id: int
    run_id: str | None
    agent_id: str | None
    tool: str
    tool_call_id: str | None
    caller_subject: str | None
    allowed: bool
    reason: str
    policy_version: str
    created_at: datetime


# ── evals ────────────────────────────────────────────────────────────────────

class EvalCaseIn(BaseModel):
    input: str = Field(min_length=1, max_length=32_000)
    expected: str = ""


class EvalSuiteIn(BaseModel):
    agent_id: str
    name: str = "suite"
    cases: list[EvalCaseIn] = Field(min_length=1, max_length=100)


class EvalSuiteOut(ORMModel):
    id: str
    agent_id: str
    name: str
    created_at: datetime


class EvalRunIn(BaseModel):
    threshold: float = Field(default=0.45, ge=0, le=1)


# ── usage ────────────────────────────────────────────────────────────────────

class UsageOut(BaseModel):
    workspace_id: str
    plan: str
    period_start: str
    period_end: str
    llm_tokens_used: int
    monthly_token_quota: int
    remaining: int
    percent_used: float
    by_kind: dict[str, int]


# ── health ───────────────────────────────────────────────────────────────────

class HealthOut(BaseModel):
    status: str
    version: str
    provider: str
    env: str
