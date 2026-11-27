from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


# Nullable columns added after a table first shipped. ``create_all`` never alters an
# existing table, so these are added in place on startup.
_ADDED_COLUMNS = {
    "policy_decisions": ("tool_call_id", "caller_subject"),
}
_ADDED_INDEXES = {
    "ix_policy_decisions_tool_call_id": ("policy_decisions", "tool_call_id"),
}


def _add_missing_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    for table_name, columns in _ADDED_COLUMNS.items():
        if not inspector.has_table(table_name):
            continue
        present = {col["name"] for col in inspector.get_columns(table_name)}
        table = Base.metadata.tables[table_name]
        for name in columns:
            if name in present:
                continue
            column_type = table.c[name].type.compile(dialect=sync_conn.dialect)
            if sync_conn.dialect.name == "postgresql":
                # Safe if another process added it first.
                sync_conn.execute(text(
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {name} {column_type}"
                ))
                continue
            try:
                with sync_conn.begin_nested():
                    sync_conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}")
                    )
            except OperationalError:
                # Another process may have added it first; anything else is a real error.
                fresh = {col["name"] for col in inspect(sync_conn).get_columns(table_name)}
                if name not in fresh:
                    raise
    for index_name, (table_name, column) in _ADDED_INDEXES.items():
        if inspector.has_table(table_name):
            sync_conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({column})"
            ))


async def init_db() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
