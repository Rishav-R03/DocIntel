"""``POST /api/v1/extract``: upload a document for extraction (FR-01)."""

import time
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from docintel.api.errors import problem_responses
from docintel.api.middleware import CLIENT_ID_HEADER, safe_id
from docintel.api.resources import Resources
from docintel.domain.errors import InvalidRequestError
from docintel.domain.job import JobStatus
from docintel.extraction.pipeline import ExtractionOutcome
from docintel.ingestion.upload import FILE_FIELD, read_upload
from docintel.persistence.models import Job
from docintel.schemas.common import ExtractionWarning
from docintel.schemas.registry import get_schema
from docintel.settings import Settings

router = APIRouter(prefix="/api/v1", tags=["extraction"])

_ALLOWED_FIELDS = frozenset({"schema_id", "options"})

# The body is parsed by our streaming reader rather than FastAPI parameters, so the multipart
# contract is declared for OpenAPI explicitly.
_UPLOAD_REQUEST_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [FILE_FIELD, "schema_id"],
                    "properties": {
                        FILE_FIELD: {
                            "type": "string",
                            "format": "binary",
                            "description": "PDF, PNG or JPEG; max 25 MB (type is sniffed).",
                        },
                        "schema_id": {"type": "string", "examples": ["invoice.v1"]},
                        "options": {
                            "type": "string",
                            "description": "Reserved JSON object of extraction options.",
                        },
                    },
                }
            }
        },
    }
}


class SubmitResponse(BaseModel):
    """``mode=async`` (202): the job is queued; poll ``status_url``."""

    job_id: uuid.UUID
    status: JobStatus
    status_url: str


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    estimated_cost_usd: Decimal


class TimingsOut(BaseModel):
    upload_ms: int
    gemini_ms: int
    validation_ms: int
    total_ms: int


class SyncExtractionResponse(BaseModel):
    """``mode=sync`` (200): the validated extraction result."""

    job_id: uuid.UUID
    schema_id: str
    status: JobStatus
    model: str
    document_validity_score: float  # adjusted: model score x rule penalties x critical confidence
    model_reported_validity: float  # the model's raw self-assessment, for transparency
    data: dict[str, Any]
    warnings: list[ExtractionWarning]
    low_confidence_fields: list[str]
    usage: UsageOut
    timings_ms: TimingsOut

    @classmethod
    def build(
        cls, job: Job, outcome: ExtractionOutcome, *, upload_ms: int, total_ms: int
    ) -> "SyncExtractionResponse":
        return cls(
            job_id=job.id,
            schema_id=outcome.schema_id,
            status=JobStatus.COMPLETED,
            model=outcome.attempts[-1].model,
            document_validity_score=outcome.validity.adjusted,
            model_reported_validity=outcome.validity.model_reported,
            data=outcome.data,
            warnings=outcome.warnings,
            low_confidence_fields=list(outcome.validity.low_confidence_fields),
            usage=UsageOut(
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                thinking_tokens=outcome.thought_tokens,
                estimated_cost_usd=outcome.cost_usd,
            ),
            timings_ms=TimingsOut(
                upload_ms=upload_ms,
                gemini_ms=outcome.gemini_ms,
                validation_ms=outcome.validation_ms,
                total_ms=total_ms,
            ),
        )


@router.post(
    "/extract",
    status_code=202,
    openapi_extra=_UPLOAD_REQUEST_BODY,
    responses={
        200: {"model": SyncExtractionResponse, "description": "mode=sync: extraction result"},
        202: {"model": SubmitResponse, "description": "mode=async: job accepted"},
        **problem_responses(400, 413, 415, 422, 429, 502, 503, 504),
    },
)
async def extract(
    request: Request,
    response: Response,
    mode: Annotated[Literal["async", "sync"], Query()] = "async",
) -> SubmitResponse | SyncExtractionResponse:
    started = time.perf_counter()
    settings: Settings = request.app.state.settings
    resources: Resources = request.app.state.resources
    upload = await read_upload(
        request,
        max_file_bytes=settings.max_upload_bytes,
        spool_bytes=settings.upload_spool_bytes,
    )
    upload_ms = int((time.perf_counter() - started) * 1000)
    try:
        unknown = upload.fields.keys() - _ALLOWED_FIELDS
        if unknown:
            raise InvalidRequestError(f"Unknown form fields: {', '.join(sorted(unknown))}.")
        schema_id = upload.fields.get("schema_id")
        if not schema_id:
            raise InvalidRequestError("Form field 'schema_id' is required.")
        schema = get_schema(schema_id)
        request_id = structlog.contextvars.get_contextvars()["request_id"]
        client_id = safe_id(request.headers.get(CLIENT_ID_HEADER))
        if mode == "sync":
            job, outcome = await resources.ingestion.extract_sync(
                upload, schema, request_id=request_id, client_id=client_id
            )
        else:
            job = await resources.ingestion.submit_async(
                upload, schema, request_id=request_id, client_id=client_id
            )
    finally:
        upload.close()

    status_url = request.app.url_path_for("get_job", job_id=str(job.id))
    response.headers["Location"] = status_url
    if mode == "sync":
        response.status_code = 200
        return SyncExtractionResponse.build(
            job,
            outcome,
            upload_ms=upload_ms,
            total_ms=int((time.perf_counter() - started) * 1000),
        )
    return SubmitResponse(job_id=job.id, status=job.status, status_url=status_url)
