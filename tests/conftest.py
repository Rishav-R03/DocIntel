import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from docintel.api.resources import Resources, ResourcesFactory
from docintel.settings import Settings


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "env": "local",
        "version": "test",
        "db_password": "test-password",
        "db_ssl_mode": "disable",
        "s3_bucket": "test-bucket",
        "sqs_jobs_url": "http://sqs.local/000000000000/test-jobs",
    }
    return Settings(**{**defaults, **overrides})


class FakeCheck:
    def __init__(self, name: str, *, error: Exception | None = None, delay_s: float = 0) -> None:
        self.name = name
        self._error = error
        self._delay_s = delay_s

    async def __call__(self) -> None:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._error:
            raise self._error


def fake_resources(*checks: FakeCheck) -> ResourcesFactory:
    """Resources for tests that only exercise health/middleware; DB/ingestion are never touched."""

    @asynccontextmanager
    async def factory(_: Settings) -> AsyncIterator[Resources]:
        yield Resources(
            health_checks=checks,
            session_factory=cast(Any, None),
            ingestion=cast(Any, None),
        )

    return factory


@pytest.fixture
def settings() -> Settings:
    return make_settings()
