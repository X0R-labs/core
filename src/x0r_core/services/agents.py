from __future__ import annotations

import json
import logging
from time import perf_counter

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ..guard import Caller
from ..models import Agent, PolicyDecision, Run, Workspace
from ..policy import POLICY_VERSION, authorize_tool, policy_tool_call_id
from ..providers import Completion, Provider, Usage, build_provider
from ..tools import ToolContext
from ..tools import resolve as resolve_tools
from ..tools import schemas as tool_schemas
from . import usage as usage_service

logger = logging.getLogger(__name__)


class DecisionNotSaved(Exception):
    def __init__(self, run_id: str | None) -> None:
        super().__init__("The policy decision could not be saved.")
        # None when nothing of the run was saved yet (the first decision failed).
        self.run_id = run_id


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
    caller: Caller | None = None,
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
    run_id = run.id
    run_saved = False

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
                    registry={tool.name: tool for tool in tools},
                    caller=caller,
                    tool_call_id=call.id,
                )
                steps.append({
                    "type": "policy", "version": POLICY_VERSION, "tool": call.name,
                    "tool_call_id": call.id,
                    "allowed": decision.allowed, "reason": decision.reason,
                    "workspace_id": workspace.id,
                    "caller_subject": caller.subject if caller else None,
                })
                session.add(PolicyDecision(
                    workspace_id=workspace.id, run_id=run.id, agent_id=agent.id,
                    tool=call.name[:120],
                    tool_call_id=policy_tool_call_id(call.id) if call.id else None,
                    caller_subject=caller.subject[:200] if caller else None,
                    allowed=decision.allowed, reason=decision.reason,
                    policy_version=POLICY_VERSION,
                ))
                # Commit before the handler runs, so the decision survives even if the
                # process dies mid-action. This also commits the run as "running", along
                # with any other pending work in this session.
                run.steps = list(steps)
                run.prompt_tokens = totals.prompt_tokens
                run.completion_tokens = totals.completion_tokens
                try:
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    if run_saved:
                        # The rollback also discarded progress since the previous decision.
                        # Try to save that progress without the failed policy step.
                        try:
                            await session.execute(update(Run).where(Run.id == run_id).values(
                                steps=steps[:-1],
                                prompt_tokens=totals.prompt_tokens,
                                completion_tokens=totals.completion_tokens,
                            ))
                            await session.commit()
                        except Exception:
                            logger.warning("Could not save progress for run %s", run_id,
                                           exc_info=True)
                            await session.rollback()
                    raise DecisionNotSaved(run_id if run_saved else None) from exc
                run_saved = True
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
    except DecisionNotSaved:
        raise
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
