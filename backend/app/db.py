"""Async SQLModel engine + session helpers."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from .config import settings

log = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)

SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create tables on boot (§10: SQLite auto-creates tables)."""
    # Import for side effect: registers the tables on SQLModel.metadata.
    from . import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn: Any) -> None:
    """Add columns that exist on the models but not yet in the database.

    `create_all` only creates missing *tables*; it will not touch a table that
    already exists. Without this, adding a field to a model leaves anyone with
    an existing `split_inference.db` hitting `no such column` on boot -- and
    since this service is deployed by pulling and restarting, that is the
    normal case rather than the exception.

    Deliberately additive only: no drops, no type changes, no data movement.
    Anything beyond adding a nullable/defaulted column needs a real migration
    tool, and the guard below makes that obvious rather than silent.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())

    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            if not (column.nullable or column.default is not None or column.server_default):
                log.warning(
                    "cannot auto-add NOT NULL column %s.%s without a default; "
                    "migrate by hand", table.name, column.name,
                )
                continue

            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(conn.dialect)}"
            default = getattr(column.default, "arg", None)
            if default is not None and not callable(default):
                literal = f"'{default}'" if isinstance(default, str) else int(default) if isinstance(default, bool) else default
                ddl += f" DEFAULT {literal}"
            conn.execute(text(ddl))
            log.info("migrated: added column %s.%s", table.name, column.name)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency."""
    async with SessionFactory() as session:
        yield session


async def dispose_db() -> None:
    await engine.dispose()
