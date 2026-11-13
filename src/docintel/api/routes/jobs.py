"""Job status, result (FR-11) and live progress over Server-Sent Events (FR-07)."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from docintel.api.errors import problem_responses
from docintel.api.middleware import CLIENT_ID_HEADER, safe_id
from docintel.api.resources import Resources
from docintel.domain.errors import DependencyUnavailableError, JobNotFoundError
from docintel.domain.job import TERMINAL_STATUSES, EventStage, JobMode, JobStatus
from docintel.persistence.errors import database_errors_as_unavailable
from docintel.persistence.models import Job, JobEvent
from docintel.persistence.repositories import (
    AuditRepository,
    EventRepository,
    JobRepository,
    ResultRepository,
)
from docintel.schemas.common import ExtractionWarning
from docintel.settings import Settings

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

_TERMINAL_STAGES = {EventStage.COMPLETED.value, EventStage.FAILED.value}


class JobError(BaseModel):
    code: str
    detail: str | None = None


class JobUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    estimated_cost_usd: Decimal


class JobResult(BaseModel):
    model: str | None
    document_validity_score: float
    model_reported_validity: float | None
    data: dict[str, Any]
    warnings: list[ExtractionWarning]
    usage: JobUsage
    model_ms: int


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    mode: JobMode
    schema_id: str
    mime_type: str
    size_bytes: int
    page_count: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    attempt_count: int
    error: JobError | None
    result: JobResult | None  # present once COMPLETED

    @classmethod
    def from_job(cls, job: Job, result: JobResult | None = None) -> "JobStatusResponse":
        return cls(
            job_id=job.id,
            status=job.status,
            mode=job.mode,
            schema_id=job.schema_id,
            mime_type=job.mime_type,
            size_bytes=job.size_bytes,
            page_count=job.page_count,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            duration_ms=job.duration_ms,
            attempt_count=job.attempt_count,
            error=JobError(code=job.error_code, detail=job.error_detail)
            if job.error_code
            else None,
            result=result,
        )


async def _visible_job(request: Request, job_id: uuid.UUID) -> Job:
    resources: Resources = request.app.state.resources
    with database_errors_as_unavailable():
        async with resources.session_factory() as session:
            job = await JobRepository(session).get(job_id)
    # Defense in depth on top of unguessable IDs: a job submitted with a client id is only
    # visible to that client. 404 (not 403) so existence isn't leaked either.
    caller = safe_id(request.headers.get(CLIENT_ID_HEADER))
    if job is None or (job.client_id is not None and job.client_id != caller):
        raise JobNotFoundError()
    return job


@router.get("/{job_id}", responses=problem_responses(400, 404, 503))
async def get_job(job_id: uuid.UUID, request: Request) -> JobStatusResponse:
    job = await _visible_job(request, job_id)
    if job.status != JobStatus.COMPLETED:
        return JobStatusResponse.from_job(job)
    resources: Resources = request.app.state.resources
    with database_errors_as_unavailable():
        async with resources.session_factory() as session:
            row = await ResultRepository(session).get(job.id)
            usage = await AuditRepository(session).usage(job.id)
    result = None
    if row is not None:
        result = JobResult(
            model=usage.model,
            document_validity_score=float(row.document_validity_score),
            model_reported_validity=(
                float(row.model_reported_validity)
                if row.model_reported_validity is not None
                else None
            ),
            data=row.result,
            warnings=[ExtractionWarning.model_validate(w) for w in row.warnings],
            usage=JobUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                thinking_tokens=usage.thinking_tokens,
                estimated_cost_usd=usage.cost_usd,
            ),
            model_ms=usage.model_ms,
        )
    return JobStatusResponse.from_job(job, result)


# ----------------------------------------------------------------------------- SSE
def _sse(event: JobEvent, status_url: str) -> str:
    name = {"COMPLETED": "completed", "FAILED": "failed"}.get(event.stage, "stage")
    data: dict[str, Any] = {
        "job_id": str(event.job_id),
        "stage": event.stage,
        "ts": event.created_at.isoformat(),
        **(event.payload or {}),
    }
    if event.stage in _TERMINAL_STAGES:
        data["result_url"] = status_url
    return f"id: {event.id}\nevent: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.get(
    "/{job_id}/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Server-Sent Events: `stage` while running, then `completed` or "
            "`failed`. Supports `Last-Event-ID` to resume.",
        },
        **problem_responses(400, 404, 503),
    },
)
async def stream_job(
    job_id: uuid.UUID,
    request: Request,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    job = await _visible_job(request, job_id)  # 404 before the stream starts
    resources: Resources = request.app.state.resources
    settings: Settings = request.app.state.settings
    status_url = request.app.url_path_for("get_job", job_id=str(job.id))
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    already_terminal = job.status in TERMINAL_STATUSES

    async def events() -> AsyncIterator[str]:
        nonlocal after
        stop_at = time.monotonic() + settings.sse_max_duration_s
        # First byte immediately (TTFB target < 300 ms): reconnect hint for EventSource clients.
        yield "retry: 3000\n\n"
        with resources.events.subscribe(job.id) as wake:
            while True:
                wake.clear()  # before reading: a NOTIFY during the read triggers another pass
                try:
                    with database_errors_as_unavailable():
                        async with resources.session_factory() as session:
                            new = await EventRepository(session).list_for_job(
                                job.id, after_id=after
                            )
                except DependencyUnavailableError:
                    new = []
                    yield ": database unavailable, retrying\n\n"
                for event in new:
                    after = event.id
                    yield _sse(event, status_url)
                    if event.stage in _TERMINAL_STAGES:
                        return
                if already_terminal:
                    return  # resumed after the end: nothing more will ever arrive
                remaining = stop_at - time.monotonic()
                if remaining <= 0:
                    yield ": max stream duration reached; reconnect with Last-Event-ID\n\n"
                    return
                try:
                    await asyncio.wait_for(
                        wake.wait(), timeout=min(settings.sse_heartbeat_s, remaining)
                    )
                except TimeoutError:
                    yield ": keep-alive\n\n"  # also the polling fallback if LISTEN is down

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
