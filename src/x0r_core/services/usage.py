from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import UsageEvent, Workspace


class QuotaExceeded(Exception):
    def __init__(self, used: int, quota: int) -> None:
        super().__init__(f"Monthly token quota exceeded ({used}/{quota}).")
        self.used = used
        self.quota = quota


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def record(
    session: AsyncSession,
    workspace_id: str,
    kind: str,
    quantity: int,
    meta: dict | None = None,
) -> None:
    if quantity <= 0:
        return
    session.add(
        UsageEvent(workspace_id=workspace_id, kind=kind, quantity=quantity, meta=meta or {})
    )


async def month_total(session: AsyncSession, workspace_id: str, kind: str = "llm_tokens") -> int:
    start = month_start()
    value = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.workspace_id == workspace_id,
                UsageEvent.kind == kind,
                UsageEvent.created_at >= start,
            )
        )
    ).scalar_one()
    return int(value or 0)


async def assert_within_quota(session: AsyncSession, workspace: Workspace) -> None:
    used = await month_total(session, workspace.id)
    if workspace.monthly_token_quota and used >= workspace.monthly_token_quota:
        raise QuotaExceeded(used, workspace.monthly_token_quota)


async def summary(session: AsyncSession, workspace: Workspace) -> dict:
    start = month_start()
    rows = (
        await session.execute(
            select(UsageEvent.kind, func.sum(UsageEvent.quantity))
            .where(
                UsageEvent.workspace_id == workspace.id,
                UsageEvent.created_at >= start,
            )
            .group_by(UsageEvent.kind)
        )
    ).all()

    by_kind = {kind: int(total or 0) for kind, total in rows}
    used = by_kind.get("llm_tokens", 0)
    quota = workspace.monthly_token_quota
    return {
        "workspace_id": workspace.id,
        "plan": workspace.plan,
        "period_start": start.isoformat(),
        "period_end": (start + timedelta(days=32)).replace(day=1).isoformat(),
        "llm_tokens_used": used,
        "monthly_token_quota": quota,
        "remaining": max(0, quota - used),
        "percent_used": round(100 * used / quota, 2) if quota else 0.0,
        "by_kind": by_kind,
    }
