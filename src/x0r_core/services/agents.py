from __future__ import annotations

import json
from time import perf_counter

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Run, Workspace
from ..policy import authorize_tool
from ..providers import Completion, Provider, Usage, build_provider
from ..tools import ToolContext
from ..tools import resolve as resolve_tools
from ..tools import schemas as tool_schemas
from . import usage as usage_service


class AgentRunError(Exception):
    def __init__(self, run: Run, message: str) -> None:
        super().__init__(message)
        self.run = run


def _tool_specs(names: list[str]) -> list[dict]:
    return tool_schemas(names)


async def run_agent(
    session: AsyncSession,
    workspace: Workspace,
    agent: Agent,
    user_input: str,
    *,
    provider: Provider | None = None,
) -> Run:
    await usage_service.assert_within_quota(session, workspace)

    active = provider or build_provider(agent.provider)
    tools = resolve_tools(agent.tools or [])
    specs = _tool_specs(agent.tools or [])

    messages: list[dict] = [
        {"role": "system", "content": agent.system_prompt},
        {"role": "user", "content": user_input},
    ]
    steps: list[dict] = []
    totals = Usage()
    started = perf_counter()

    run = Run(
        workspace_id=workspace.id,
        agent_id=agent.id,
        status="running",
        input=user_input,
    )
    session.add(run)
    await session.flush()

    final = ""
    outcome = "succeeded"
    try:
        for _ in range(max(1, agent.max_steps)):
            completion: Completion = await active.complete(
                messages=messages,
                tools=specs or None,
                model=agent.model,
                temperature=agent.temperature,
            )
            totals = totals + completion.usage

            message: dict = {"role": "assistant", "content": completion.content}
            if completion.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in completion.tool_calls
                ]
            messages.append(message)

            steps.append(
                {
                    "type": "model",
                    "content": completion.content,
                    "tool_calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in completion.tool_calls
                    ],
                    "usage": {
                        "prompt_tokens": completion.usage.prompt_tokens,
                        "completion_tokens": completion.usage.completion_tokens,
                    },
                }
            )

            if not completion.tool_calls:
                final = completion.content or ""
                break

            if len(completion.tool_calls) > _settings().max_tool_calls_per_step:
                outcome = "limit_exceeded"
                final = "Stopped: too many tool calls in one step."
                steps.append({"type": "limit", "reason": "tool_call_limit"})
                break

            by_name = {tool.name: tool for tool in tools}
            ctx = ToolContext(session=session, workspace_id=workspace.id, settings=_settings())
            for call in completion.tool_calls:
                decision = authorize_tool(
                    workspace_id=workspace.id,
                    agent_workspace_id=agent.workspace_id,
                    agent_tools=agent.tools or [],
                    tool_name=call.name,
                    arguments=call.arguments,
                    settings=ctx.settings,
                )
                steps.append({
                    "type": "policy", "version": "1", "tool": call.name,
                    "allowed": decision.allowed, "reason": decision.reason,
                    "workspace_id": workspace.id,
                })
                if not decision.allowed:
                    outcome = "blocked"
                    final = "Stopped: tool action denied by execution policy."
                    break
                tool = by_name.get(call.name)
                if tool is None:
                    result: object = {"error": f"unknown tool {call.name!r}"}
                else:
                    try:
                        result = await tool.handler(ctx, **call.arguments)
                    except TypeError as exc:
                        result = {"error": f"bad arguments: {exc}"}
                    except Exception as exc:  # noqa: BLE001 - tool failure is data, not a crash
                        result = {"error": str(exc)}
                steps.append(
                    {
                        "type": "tool",
                        "tool": call.name,
                        "arguments": call.arguments,
                        "result": result,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": _bounded(json.dumps(result, default=str)),
                    }
                )
            if outcome == "blocked":
                break
        else:
            outcome = "limit_exceeded"
            final = "Stopped: the agent reached its step limit before finishing."
            steps.append({"type": "limit", "max_steps": agent.max_steps})

        run.status = outcome
        run.output = final
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.error = str(exc)
        run.output = ""
        steps.append({"type": "error", "message": str(exc)})
        run.steps = steps
        run.prompt_tokens = totals.prompt_tokens
        run.completion_tokens = totals.completion_tokens
        run.latency_ms = int((perf_counter() - started) * 1000)
        await usage_service.record(
            session,
            workspace.id,
            "llm_tokens",
            totals.total,
            {"run_id": run.id, "agent_id": agent.id, "provider": active.name},
        )
        await session.flush()
        raise AgentRunError(run, str(exc)) from exc

    run.steps = steps
    run.prompt_tokens = totals.prompt_tokens
    run.completion_tokens = totals.completion_tokens
    run.latency_ms = int((perf_counter() - started) * 1000)

    await usage_service.record(
        session,
        workspace.id,
        "llm_tokens",
        totals.total,
        {"run_id": run.id, "agent_id": agent.id, "provider": active.name},
    )
    await session.flush()
    return run


def _bounded(text: str, limit: int = 8000) -> str:
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _settings():
    from ..config import get_settings

    return get_settings()
