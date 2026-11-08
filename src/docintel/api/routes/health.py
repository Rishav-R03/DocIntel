"""Liveness and readiness endpoints.

``/health/live``  - no I/O; used by the ECS container health check. A slow dependency must never
                    make ECS kill otherwise healthy tasks.
``/health/ready`` - used by the ALB. Reports dependency status but still answers 200 when a
                    dependency is down ("degraded"): failing every task's health check on a shared
                    dependency outage would pull the whole fleet out of the target group, which
                    defeats graceful degradation (implementation.md §4.10.4).
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from docintel.api.resources import Resources
from docintel.health import run_checks
from docintel.settings import Settings

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class DependencyStatus(BaseModel):
    status: Literal["ok", "unavailable"]
    latency_ms: int
    error: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    checks: dict[str, DependencyStatus]


@router.get("/live")
async def live() -> LivenessResponse:
    return LivenessResponse()


@router.get("/ready")
async def ready(request: Request) -> ReadinessResponse:
    settings: Settings = request.app.state.settings
    resources: Resources = request.app.state.resources
    results = await run_checks(resources.health_checks, settings.health_check_timeout_s)
    return ReadinessResponse(
        status="ok" if all(r.ok for r in results) else "degraded",
        version=settings.version,
        checks={
            r.name: DependencyStatus(
                status="ok" if r.ok else "unavailable", latency_ms=r.latency_ms, error=r.error
            )
            for r in results
        },
    )
