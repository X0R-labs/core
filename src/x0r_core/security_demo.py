"""Deterministic mock testing of our experimental execution boundary."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace

from .config import get_settings
from .db import dispose_db, get_session_factory, init_db
from .models import Agent, Workspace
from .providers.mock import MockProvider
from .services.agents import run_agent
from .services.ingest import ingest_document
from .tools import REGISTRY

QUESTION = "How long is the synthetic warranty?"
DOCUMENT = "The synthetic warranty covers manufacturing defects for 24 months."


def run_isolated() -> None:
    """CLI only: isolate configuration, registry instrumentation and ephemeral data."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("X0R_")}
    env.update(X0R_PROVIDER="mock", X0R_DATABASE_URL="sqlite+aiosqlite:///:memory:")
    with tempfile.TemporaryDirectory(prefix="x0r-security-demo-") as directory:
        subprocess.run(
            [sys.executable, "-I", "-m", "x0r_core.security_demo"],
            cwd=directory, env=env, check=True,
        )


async def demonstrate() -> dict:
    """Worker entry point; invoke through run_isolated, never inside an API server."""
    get_settings.cache_clear()
    settings = get_settings()
    # Explicit settings as well as the isolated process environment. No .env is present.
    settings.provider = "mock"
    settings.database_url = "sqlite+aiosqlite:///:memory:"
    settings.http_tool_allowlist = ""
    original = REGISTRY["knowledge_search"]
    handler_calls = 0

    async def counted_handler(*args, **kwargs):
        nonlocal handler_calls
        handler_calls += 1
        return await original.handler(*args, **kwargs)

    REGISTRY["knowledge_search"] = replace(original, handler=counted_handler)
    try:
        await init_db()
        async with get_session_factory()() as session:
            workspace = Workspace(name="Synthetic security demonstration", plan="demo")
            session.add(workspace)
            await session.flush()
            await ingest_document(
                session, workspace, title="Synthetic warranty", content=DOCUMENT,
                provider=MockProvider(), embed=False,
            )
            agent = Agent(
                workspace_id=workspace.id, name="Synthetic lookup", provider="mock",
                system_prompt="Answer from the synthetic knowledge base.",
                tools=["knowledge_search"], max_steps=2,
            )
            session.add(agent)
            await session.flush()
            cases = []
            for allowed in (False, True):
                # Operator-controlled configuration, never derived from the user/model.
                settings.operator_tool_allowlist = (
                    "knowledge_search,calculator" if allowed else "calculator"
                )
                before = handler_calls
                run = await run_agent(
                    session, workspace, agent, QUESTION, provider=MockProvider()
                )
                policies = [step for step in run.steps if step["type"] == "policy"]
                executions = [step for step in run.steps if step["type"] == "tool"]
                case = {
                    "case": "permitted" if allowed else "operator_denied",
                    "operator_tool_allowlist": settings.operator_tool_allowlist,
                    "agent_tools": agent.tools,
                    "requested_tool": "knowledge_search",
                    "status": run.status,
                    "policy": [
                        {key: step[key] for key in ("version", "tool", "allowed", "reason")}
                        for step in policies
                    ],
                    "handler_calls": handler_calls - before,
                    "tool_steps": len(executions),
                    "answer_contains_24_months": "24 months" in run.output,
                }
                reason = "granted" if allowed else "not_granted_by_operator"
                if (
                    run.status != ("succeeded" if allowed else "blocked")
                    or len(policies) != 1 or policies[0]["reason"] != reason
                    or policies[0]["allowed"] != allowed
                    or case["handler_calls"] != int(allowed)
                    or len(executions) != int(allowed)
                    or case["answer_contains_24_months"] != allowed
                    or any("arguments" in step for step in policies)
                ):
                    raise RuntimeError("Security demonstration checks failed")
                cases.append(case)
            return {
                "demonstration": "deterministic mock testing",
                "system": "our own experimental X0R Core; not a client audit",
                "provider": "mock",
                "data": "synthetic; temporary in-memory database",
                "cases": cases,
                "checks": "passed",
            }
    finally:
        REGISTRY["knowledge_search"] = original
        await dispose_db()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(demonstrate()), indent=2))
