"""Dependency health checks used by ``GET /health/ready``."""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class HealthCheck(Protocol):
    name: str

    async def __call__(self) -> None:
        """Return normally when healthy; raise otherwise."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    latency_ms: int
    error: str | None = None


class DatabaseCheck:
    name = "database"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def __call__(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))


class S3BucketCheck:
    name = "s3"

    def __init__(self, s3_client: Any, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    async def __call__(self) -> None:
        await self._s3.head_bucket(Bucket=self._bucket)


async def _run_one(check: HealthCheck, timeout_s: float) -> CheckResult:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(check(), timeout=timeout_s)
    except Exception as exc:
        # Exception type only: messages can contain hostnames or credentials fragments.
        error = "Timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
        return CheckResult(check.name, False, _elapsed_ms(start), error)
    return CheckResult(check.name, True, _elapsed_ms(start))


async def run_checks(checks: Sequence[HealthCheck], timeout_s: float) -> list[CheckResult]:
    return list(await asyncio.gather(*(_run_one(c, timeout_s) for c in checks)))


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
