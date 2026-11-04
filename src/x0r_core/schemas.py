from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    content: str = Field(min_length=1)
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
    query: str = Field(min_length=1)
    k: int = Field(default=5, ge=1, le=20)


class SearchOut(BaseModel):
    query: str
    results: list[dict]
    count: int


# ── agents / runs ────────────────────────────────────────────────────────────

class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    system_prompt: str = "You are a helpful assistant."
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


class RunIn(BaseModel):
    input: str = Field(min_length=1)


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


# ── evals ────────────────────────────────────────────────────────────────────

class EvalCaseIn(BaseModel):
    input: str = Field(min_length=1)
    expected: str = ""


class EvalSuiteIn(BaseModel):
    agent_id: str
    name: str = "suite"
    cases: list[EvalCaseIn] = Field(min_length=1)


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
