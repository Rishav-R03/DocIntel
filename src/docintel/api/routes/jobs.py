"""``GET /api/v1/jobs/{job_id}``: job status (FR-11; results are added in Phase 4)."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from docintel.api.errors import problem_responses
from docintel.api.middleware import CLIENT_ID_HEADER, safe_id
from docintel.api.resources import Resources
from docintel.domain.errors import JobNotFoundError
from docintel.domain.job import JobMode, JobStatus
from docintel.persistence.errors import database_errors_as_unavailable
from docintel.persistence.models import Job
from docintel.persistence.repositories import JobRepository

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class JobError(BaseModel):
    code: str
    detail: str | None = None


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
    error: JobError | None

    @classmethod
    def from_job(cls, job: Job) -> "JobStatusResponse":
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
            error=JobError(code=job.error_code, detail=job.error_detail)
            if job.error_code
            else None,
        )


@router.get("/{job_id}", responses=problem_responses(400, 404, 503))
async def get_job(job_id: uuid.UUID, request: Request) -> JobStatusResponse:
    resources: Resources = request.app.state.resources
    with database_errors_as_unavailable():
        async with resources.session_factory() as session:
            job = await JobRepository(session).get(job_id)

    # Defense in depth on top of unguessable IDs: a job submitted with a client id is only
    # visible to that client. 404 (not 403) so existence isn't leaked either.
    caller = safe_id(request.headers.get(CLIENT_ID_HEADER))
    if job is None or (job.client_id is not None and job.client_id != caller):
        raise JobNotFoundError()
    return JobStatusResponse.from_job(job)
