from __future__ import annotations

import asyncio
import json

import typer

from .config import get_settings
from .db import dispose_db, get_session_factory, init_db
from .models import Agent, ApiKey, EvalCase, EvalSuite, Workspace
from .security import generate_key
from .services import ingest as ingest_service

app = typer.Typer(
    add_completion=False,
    help="X0R Core — self-hosted AI agent backend.",
    no_args_is_help=True,
)


@app.command("security-demo")
def security_demo() -> None:
    """Deterministic mock testing: denied and permitted tool execution, offline."""
    from .security_demo import run_isolated

    run_isolated()

BANNER = r"""
  __  __  ___  ____      ____
  \ \/ / / _ \|  _ \    / ___|___  _ __ ___
   \  / | | | | |_) |  | |   / _ \| '__/ _ \
   /  \ | |_| |  _ <   | |__| (_) | | |  __/
  /_/\_\ \___/|_| \_\   \____\___/|_|  \___|
"""


def _print_key(raw: str) -> None:
    typer.secho("\nAPI key (shown once — store it now):", fg=typer.colors.YELLOW, bold=True)
    typer.secho(f"  {raw}\n", fg=typer.colors.GREEN, bold=True)


async def _create_workspace(name: str, plan: str, quota: int) -> tuple[Workspace, str]:
    await init_db()
    factory = get_session_factory()
    raw, prefix, digest = generate_key()
    async with factory() as session:
        workspace = Workspace(name=name, plan=plan, monthly_token_quota=quota)
        session.add(workspace)
        await session.flush()
        session.add(
            ApiKey(workspace_id=workspace.id, name="default", prefix=prefix, key_hash=digest)
        )
        await session.commit()
        return workspace, raw


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, help="Port."),
    reload: bool = typer.Option(False, "--reload", help="Reload on file changes (dev)."),
) -> None:
    """Run the API server."""
    import uvicorn

    typer.secho(BANNER, fg=typer.colors.BLUE)
    settings = get_settings()
    typer.echo(f"  provider : {settings.provider}")
    typer.echo(f"  database : {settings.database_url}")
    typer.echo(f"  docs     : http://{host}:{port}/docs\n")

    if reload:
        uvicorn.run("x0r_core.main:app", host=host, port=port, reload=True)
    else:
        from .main import create_app

        uvicorn.run(create_app(), host=host, port=port, log_level="info")


@app.command()
def init(
    name: str = typer.Option("Default workspace", help="Workspace name."),
    plan: str = typer.Option("trial", help="Plan label."),
    quota: int = typer.Option(1_000_000, help="Monthly token quota."),
) -> None:
    """Create the database and a first workspace + API key."""
    workspace, raw = asyncio.run(_create_workspace(name, plan, quota))
    typer.secho(f"Created workspace {workspace.id} ({workspace.name})", fg=typer.colors.GREEN)
    _print_key(raw)
    typer.echo("Try it:")
    typer.echo(f'  export X0R_KEY={raw}')
    typer.echo('  x0r serve')
    asyncio.run(dispose_db())


DEMO_DOCS = [
    (
        "Refund policy",
        "Northwind Logistics refund policy.\n\n"
        "Customers may request a full refund within 30 days of delivery. After 30 days, "
        "refunds are prorated at 50 percent up to day 60. Refunds are never available after "
        "day 60.\n\n"
        "Damaged goods must be reported within 7 days with photos. Damaged-goods claims are "
        "approved automatically when the order value is under 500 USD; above that, a claims "
        "specialist must review within two business days.\n\n"
        "Refunds are issued to the original payment method and settle in 5 to 7 business days.",
    ),
    (
        "Delivery SLAs",
        "Northwind Logistics delivery service levels.\n\n"
        "Standard delivery: 3 to 5 business days in the continental United States. Express "
        "delivery: 1 to 2 business days. International delivery: 7 to 14 business days, "
        "depending on customs clearance.\n\n"
        "If a shipment misses its promised window by more than 48 hours, the customer is "
        "entitled to a shipping-fee credit. Orders above 2,000 USD qualify for a dedicated "
        "account manager and proactive delay notifications.",
    ),
    (
        "Escalation and support hours",
        "Support hours are 08:00 to 20:00 Eastern Time, Monday through Friday.\n\n"
        "Escalate to a human when: the customer mentions legal action, the order value exceeds "
        "10,000 USD, the request involves a data deletion, or the customer has contacted us "
        "three or more times about the same issue.\n\n"
        "The escalation queue is monitored by the duty manager. Target first response for "
        "escalations is 15 minutes during support hours.",
    ),
    (
        "Onboarding checklist",
        "New account onboarding for Northwind Logistics.\n\n"
        "1. Confirm billing entity and tax ID. 2. Create warehouse locations. 3. Connect the "
        "order source (API, CSV, or Shopify). 4. Run a test shipment. 5. Schedule a 30-minute "
        "go-live review with the account manager.\n\n"
        "Most accounts go live in 5 business days. Data migration from a previous provider "
        "adds roughly one week.",
    ),
]

DEMO_PROMPT = (
    "You are the Northwind Logistics support assistant. Answer only from the knowledge base. "
    "Be concise, cite the policy you used, and escalate when the policy says to. If the "
    "knowledge base does not contain the answer, say so and offer to escalate."
)


async def _seed() -> tuple[str, str, str]:
    await init_db()
    factory = get_session_factory()
    raw, prefix, digest = generate_key()

    async with factory() as session:
        workspace = Workspace(
            name="Northwind Logistics (demo)", plan="demo", monthly_token_quota=1_000_000
        )
        session.add(workspace)
        await session.flush()
        session.add(
            ApiKey(workspace_id=workspace.id, name="demo", prefix=prefix, key_hash=digest)
        )

        for title, content in DEMO_DOCS:
            await ingest_service.ingest_document(
                session,
                workspace,
                title=title,
                content=content,
                source="seed",
            )

        agent = Agent(
            workspace_id=workspace.id,
            name="Support assistant",
            description="Answers customer questions from the Northwind knowledge base.",
            system_prompt=DEMO_PROMPT,
            tools=["knowledge_search", "calculator"],
            temperature=0.1,
        )
        session.add(agent)
        await session.flush()

        suite = EvalSuite(workspace_id=workspace.id, agent_id=agent.id, name="golden questions")
        session.add(suite)
        await session.flush()
        session.add_all(
            [
                EvalCase(
                    suite_id=suite.id,
                    input="Can I get a refund 45 days after delivery?",
                    expected="prorated 50 percent up to day 60",
                ),
                EvalCase(
                    suite_id=suite.id,
                    input="How long does express delivery take?",
                    expected="1 to 2 business days",
                ),
                EvalCase(
                    suite_id=suite.id,
                    input="When should I escalate to a human?",
                    expected="legal action, order value exceeds 10,000 USD, data deletion",
                ),
            ]
        )
        await session.commit()
        return workspace.id, agent.id, raw


@app.command()
def seed() -> None:
    """Create a demo workspace with documents, an agent and an eval suite."""
    typer.secho(BANNER, fg=typer.colors.BLUE)
    workspace_id, agent_id, raw = asyncio.run(_seed())
    typer.secho("Demo workspace ready.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  workspace : {workspace_id}")
    typer.echo(f"  agent     : {agent_id}")
    _print_key(raw)
    typer.echo("Next:")
    typer.echo(f'  export X0R_KEY={raw}')
    typer.echo("  x0r serve")
    typer.echo(
        f"  curl -s localhost:8000/v1/agents/{agent_id}/run \\\n"
        '    -H "Authorization: Bearer $X0R_KEY" -H "Content-Type: application/json" \\\n'
        '    -d \'{"input":"Can I get a refund after 45 days?"}\' | jq'
    )
    asyncio.run(dispose_db())


@app.command("keys")
def list_keys() -> None:
    """List API key prefixes for every workspace."""

    async def _run() -> None:
        await init_db()
        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            rows = (
                await session.execute(
                    select(ApiKey, Workspace).join(Workspace, ApiKey.workspace_id == Workspace.id)
                )
            ).all()
            for key, workspace in rows:
                state = "revoked" if key.revoked_at else "active"
                typer.echo(f"{workspace.name:<32} {key.prefix:<10} {key.name:<12} {state}")

    asyncio.run(_run())
    asyncio.run(dispose_db())


@app.command()
def smoke() -> None:
    """Round-trip the pipeline in-process: ingest, search, agent run, usage."""

    async def _run() -> None:
        from .providers import build_provider
        from .services import agents as agent_service
        from .services import retrieval, usage

        await init_db()
        factory = get_session_factory()
        raw, prefix, digest = generate_key()
        async with factory() as session:
            workspace = Workspace(name="smoke", plan="test")
            session.add(workspace)
            await session.flush()
            session.add(
                ApiKey(workspace_id=workspace.id, name="smoke", prefix=prefix, key_hash=digest)
            )
            doc, chunks = await ingest_service.ingest_document(
                session,
                workspace,
                title="Warranty",
                content="The warranty covers manufacturing defects for 24 months from purchase.",
            )
            hits = await retrieval.search(session, workspace.id, "how long is the warranty")
            agent = Agent(
                workspace_id=workspace.id,
                name="smoke",
                system_prompt="Answer from the knowledge base.",
                tools=["knowledge_search"],
            )
            session.add(agent)
            await session.flush()

            provider = build_provider("mock")
            run = await agent_service.run_agent(
                session, workspace, agent, "How long is the warranty?", provider=provider
            )
            totals = await usage.summary(session, workspace)
            await session.commit()

        typer.echo(json.dumps(
            {
                "chunks": chunks,
                "top_hit": hits[0].as_dict() if hits else None,
                "run_status": run.status,
                "answer": run.output,
                "tokens": totals["llm_tokens_used"],
            },
            indent=2,
        ))

    asyncio.run(_run())
    asyncio.run(dispose_db())


if __name__ == "__main__":
    app()
