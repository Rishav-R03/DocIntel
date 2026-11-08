"""Real-Postgres fixtures. One throwaway, fully migrated database per test session, so the suite
never touches the local dev database."""

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from docintel.settings import Settings, get_settings
from tests.conftest import make_settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        if os.environ.get("REQUIRE_INTEGRATION") == "1":
            pytest.fail(f"REQUIRE_INTEGRATION=1 but {name} is not set")
        pytest.skip(f"{name} not set")
    return value


@pytest.fixture(scope="session")
def moto_endpoint() -> Iterator[str]:
    """moto server (S3, SQS, KMS) running as a container: compose service locally, service
    container in CI. Same pattern as Postgres, and keeps moto's heavy deps out of the image."""
    endpoint = _require_env("DOCINTEL_TEST_AWS_ENDPOINT")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AWS_ACCESS_KEY_ID", "test")
        mp.setenv("AWS_SECRET_ACCESS_KEY", "test")
        mp.setenv("AWS_DEFAULT_REGION", "us-east-1")
        yield endpoint


def _dsn(host: str, user: str, password: str, db: str) -> str:
    return f"postgresql://{user}:{password}@{host}:5432/{db}"


@pytest.fixture(scope="session")
def db_settings() -> Iterator[Settings]:
    host = _require_env("DOCINTEL_TEST_DB_HOST")
    user = os.environ.get("DOCINTEL_TEST_DB_USER", "docintel")
    password = os.environ.get("DOCINTEL_TEST_DB_PASSWORD", "docintel")
    db_name = f"docintel_test_{uuid.uuid4().hex[:8]}"

    async def admin(sql: str) -> None:
        conn = await asyncpg.connect(_dsn(host, user, password, "postgres"))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin(f'CREATE DATABASE "{db_name}"'))
    env = {
        "DOCINTEL_DB_HOST": host,
        "DOCINTEL_DB_NAME": db_name,
        "DOCINTEL_DB_USER": user,
        "DOCINTEL_DB_PASSWORD": password,
        "DOCINTEL_DB_SSL_MODE": "disable",
        "DOCINTEL_S3_BUCKET": "unused",
        "DOCINTEL_SQS_JOBS_URL": "unused",
    }
    with pytest.MonkeyPatch.context() as mp:
        for key, value in env.items():
            mp.setenv(key, value)
        get_settings.cache_clear()  # alembic env.py reads settings from the environment
        try:
            command.upgrade(alembic_config(), "head")
            yield make_settings(db_host=host, db_name=db_name, db_user=user, db_password=password)
        finally:
            get_settings.cache_clear()
            asyncio.run(admin(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))


def fetch(settings: Settings, sql: str, *args: Any) -> list[asyncpg.Record]:
    """Run a query from synchronous test code (TestClient tests are sync)."""

    async def go() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(
            _dsn(
                settings.db_host,
                settings.db_user,
                settings.db_password.get_secret_value(),
                settings.db_name,
            )
        )
        try:
            return list(await conn.fetch(sql, *args))
        finally:
            await conn.close()

    return asyncio.run(go())
