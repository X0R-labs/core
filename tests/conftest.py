from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TEST_DIR = Path(tempfile.mkdtemp(prefix="x0r-test-"))
os.environ["X0R_DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DIR / 'test.db'}"
os.environ["X0R_PROVIDER"] = "mock"
os.environ["X0R_BOOTSTRAP_TOKEN"] = "test-bootstrap"

from httpx import ASGITransport, AsyncClient  # noqa: E402

from x0r_core.config import get_settings  # noqa: E402
from x0r_core.db import dispose_db, get_session_factory, init_db  # noqa: E402
from x0r_core.main import app  # noqa: E402
from x0r_core.models import ApiKey, Workspace  # noqa: E402
from x0r_core.security import generate_key  # noqa: E402

get_settings.cache_clear()


async def _make_workspace(name: str = "test", quota: int = 1_000_000) -> tuple[str, str]:
    factory = get_session_factory()
    raw, prefix, digest = generate_key()
    async with factory() as session:
        workspace = Workspace(name=name, plan="test", monthly_token_quota=quota)
        session.add(workspace)
        await session.flush()
        session.add(
            ApiKey(workspace_id=workspace.id, name="test", prefix=prefix, key_hash=digest)
        )
        await session.commit()
        return workspace.id, raw


@pytest.fixture
async def client():
    await init_db()
    _, raw = await _make_workspace()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw}"},
    ) as c:
        yield c
    await dispose_db()


@pytest.fixture
async def workspace_factory():
    await init_db()
    yield _make_workspace
    await dispose_db()


@pytest.fixture
async def anon_client():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await dispose_db()
