"""Migrations and DB connectivity against a real Postgres."""

import asyncio
from collections.abc import Callable

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from docintel.health import DatabaseCheck
from docintel.persistence.db import create_engine
from docintel.persistence.models import Base
from docintel.settings import Settings
from tests.integration.conftest import alembic_config

pytestmark = pytest.mark.integration

APP_TABLES = {"jobs", "job_events", "extraction_results", "llm_call_audit"}


def _run[T](settings: Settings, fn: Callable[[Connection], T]) -> T:
    """Run a sync inspection callable on a fresh async connection (tests here are sync because
    alembic's env.py calls ``asyncio.run`` itself)."""

    async def go() -> T:
        engine = create_engine(settings, role="test", poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_migrations_match_models_and_round_trip(db_settings: Settings) -> None:
    # The session fixture already upgraded to head.
    tables = set(_run(db_settings, lambda c: inspect(c).get_table_names()))
    assert tables.issuperset(APP_TABLES)

    diff = _run(
        db_settings,
        lambda c: compare_metadata(
            MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata
        ),
    )
    assert diff == [], f"ORM models and migrations have drifted: {diff}"

    command.downgrade(alembic_config(), "base")
    assert not APP_TABLES & set(_run(db_settings, lambda c: inspect(c).get_table_names()))

    command.upgrade(alembic_config(), "head")  # downgrade is genuinely reversible


async def test_database_health_check_against_real_postgres(db_settings: Settings) -> None:
    engine = create_engine(db_settings, role="test", poolclass=NullPool)
    try:
        await DatabaseCheck(engine)()
    finally:
        await engine.dispose()
