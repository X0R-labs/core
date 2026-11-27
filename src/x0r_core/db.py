from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from time import sleep

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex

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
    "policy_decisions": (
        "tool_call_id", "caller_subject", "policy_digest", "policy_revision",
        "source", "decision_key", "decided_at",
    ),
}
_ADDED_INDEXES = {
    "ix_policy_decisions_tool_call_id": "policy_decisions",
    "ix_policy_decisions_workspace_decision_key": "policy_decisions",
}
_SCHEMA_LOCK_KEY = 0x583052434F5245


def _apply_schema_change(sync_conn, statement: str, is_present: Callable[[], bool]) -> None:
    for attempt in range(4):
        try:
            with sync_conn.begin_nested():
                sync_conn.execute(text(statement))
            return
        except OperationalError as exc:
            if (sync_conn.dialect.name == "sqlite" and attempt < 3
                    and "database is locked" in str(exc.orig).lower()):
                sleep(0.05 * (attempt + 1))
                continue
            if not is_present():
                raise
            return


def _add_missing_columns(sync_conn) -> None:
    postgres = sync_conn.dialect.name == "postgresql"
    if postgres:
        sync_conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_LOCK_KEY})
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
            if postgres:
                sync_conn.execute(text(
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {name} {column_type}"
                ))
                continue
            _apply_schema_change(
                sync_conn, f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}",
                lambda name=name, table_name=table_name: (
                    name in {c["name"] for c in inspect(sync_conn).get_columns(table_name)}
                ),
            )
    for index_name, table_name in _ADDED_INDEXES.items():
        if inspector.has_table(table_name):
            table = Base.metadata.tables[table_name]
            index = next(index for index in table.indexes if index.name == index_name)
            statement = str(CreateIndex(index, if_not_exists=True).compile(
                dialect=sync_conn.dialect,
            ))
            if postgres:
                sync_conn.execute(text(statement))
            else:
                _apply_schema_change(
                    sync_conn, statement,
                    lambda index_name=index_name, table_name=table_name: (
                        index_name in {
                            i["name"] for i in inspect(sync_conn).get_indexes(table_name)
                        }
                    ),
                )
    if inspector.has_table("policy_decisions"):
        # Rows from before ``source`` existed were all made by Core runs.
        sync_conn.execute(text(
            "UPDATE policy_decisions SET source = 'core' "
            "WHERE source IS NULL AND decision_key IS NULL"
        ))


async def init_db() -> None:
    async with get_engine().begin() as conn:
        if conn.dialect.name == "postgresql":
            # Keep the advisory lock through create_all, including on a fresh database.
            await conn.run_sync(_add_missing_columns)
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name != "postgresql":
            await conn.run_sync(_add_missing_columns)


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
