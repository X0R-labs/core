from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, EvalCase, EvalSuite, Workspace
from .agents import AgentRunError, run_agent

_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def similarity(output: str, expected: str) -> float:
    """Recall of the expected terms: did the answer cover what it had to cover?

    Deliberately simple and deterministic. An LLM judge drops in behind the same
    function signature when a customer needs semantic grading.
    """
    if not expected.strip():
        return 1.0 if output.strip() else 0.0
    a, b = _terms(output), _terms(expected)
    if not a or not b:
        return 0.0
    return len(a & b) / len(b)


async def run_suite(
    session: AsyncSession,
    workspace: Workspace,
    suite: EvalSuite,
    *,
    threshold: float = 0.45,
) -> dict:
    agent = await session.get(Agent, suite.agent_id)
    if agent is None:
        raise ValueError("suite points at a missing agent")

    cases = (
        (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite.id))).scalars()
    ).all()

    results = []
    passed = 0
    for case in cases:
        try:
            run = await run_agent(session, workspace, agent, case.input)
        except AgentRunError as exc:
            run = exc.run
        score = similarity(run.output, case.expected) if run.status == "succeeded" else 0.0
        ok = run.status == "succeeded" and score >= threshold
        passed += int(ok)
        case.last_output = run.output
        case.score = round(score, 4)
        case.passed = ok
        results.append(
            {
                "case_id": case.id,
                "input": case.input,
                "expected": case.expected,
                "output": run.output,
                "score": round(score, 4),
                "passed": ok,
                "run_id": run.id,
                "status": run.status,
            }
        )

    total = len(cases)
    return {
        "suite_id": suite.id,
        "agent_id": agent.id,
        "threshold": threshold,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "results": results,
    }
