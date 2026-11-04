from __future__ import annotations

from datetime import UTC, datetime
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .models import ApiKey, Workspace
from .security import hash_key, parse_prefix

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_api_key(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> ApiKey:
    raw = x_api_key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Send 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    prefix = parse_prefix(raw)
    if not prefix:
        raise HTTPException(status_code=401, detail="Malformed API key.")

    key = (
        await session.execute(
            select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.key_hash == hash_key(raw))
        )
    ).scalar_one_or_none()

    if key is None or key.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key.")

    key.last_used_at = datetime.now(UTC)
    return key


ApiKeyDep = Annotated[ApiKey, Depends(get_api_key)]


async def get_workspace(session: SessionDep, key: ApiKeyDep) -> Workspace:
    workspace = await session.get(Workspace, key.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=401, detail="Workspace no longer exists.")
    return workspace


WorkspaceDep = Annotated[Workspace, Depends(get_workspace)]


async def require_bootstrap(
    x_bootstrap_token: Annotated[str | None, Header()] = None,
) -> None:
    expected = get_settings().bootstrap_token
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="Workspace creation is disabled. Set X0R_BOOTSTRAP_TOKEN or use the CLI.",
        )
    if not compare_digest((x_bootstrap_token or "").encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Invalid bootstrap token.")
