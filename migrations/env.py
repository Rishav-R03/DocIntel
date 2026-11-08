"""Alembic environment: async engine built from the same Settings the app uses."""

import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from docintel.logging import configure_logging
from docintel.persistence.db import create_engine
from docintel.persistence.models import Base
from docintel.settings import get_settings

# Arbitrary constant shared by every migration run; serialises concurrent `alembic upgrade`
# (e.g. two deploy pipelines racing) instead of letting them interleave DDL.
MIGRATION_LOCK_ID = 7_301_984_221

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    # Default is one transaction for the whole run; Postgres DDL is transactional, so a failed
    # migration rolls back completely instead of leaving a half-migrated schema.
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        context.run_migrations()


async def _run_online() -> None:
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine(settings, role="migrate", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_configure)
            await connection.commit()
    finally:
        await engine.dispose()


def _run_offline() -> None:
    """``alembic upgrade head --sql``: render SQL for review without a database."""
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _run_offline()
else:
    asyncio.run(_run_online())
