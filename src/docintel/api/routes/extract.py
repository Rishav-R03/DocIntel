"""``POST /api/v1/extract``: upload a document for extraction (FR-01)."""

import uuid
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from docintel.api.middleware import CLIENT_ID_HEADER, safe_id
from docintel.api.resources import Resources
from docintel.domain.errors import InvalidRequestError, NotImplementedYetError
from docintel.domain.job import JobStatus
from docintel.ingestion.upload import FILE_FIELD, read_upload
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
    job_id: uuid.UUID
    status: JobStatus
    status_url: str


@router.post(
    "/extract",
    status_code=202,
    openapi_extra=_UPLOAD_REQUEST_BODY,
    responses={
        400: {"description": "Malformed request"},
        413: {"description": "File larger than the limit"},
        415: {"description": "Not a PDF/PNG/JPEG"},
        422: {"description": "Unknown schema, encrypted PDF, too many pages, unreadable file"},
        503: {"description": "Storage, database or queue temporarily unavailable"},
    },
)
async def extract(
    request: Request,
    response: Response,
    mode: Annotated[Literal["async", "sync"], Query()] = "async",
) -> SubmitResponse:
    if mode == "sync":  # checked before reading the body
        raise NotImplementedYetError("Synchronous extraction is not available yet; use mode=async.")

    settings: Settings = request.app.state.settings
    resources: Resources = request.app.state.resources
    upload = await read_upload(
        request,
        max_file_bytes=settings.max_upload_bytes,
        spool_bytes=settings.upload_spool_bytes,
    )
    try:
        unknown = upload.fields.keys() - _ALLOWED_FIELDS
        if unknown:
            raise InvalidRequestError(f"Unknown form fields: {', '.join(sorted(unknown))}.")
        schema_id = upload.fields.get("schema_id")
        if not schema_id:
            raise InvalidRequestError("Form field 'schema_id' is required.")
        job = await resources.ingestion.submit_async(
            upload,
            get_schema(schema_id),
            request_id=structlog.contextvars.get_contextvars()["request_id"],
            client_id=safe_id(request.headers.get(CLIENT_ID_HEADER)),
        )
    finally:
        upload.close()

    status_url = request.app.url_path_for("get_job", job_id=str(job.id))
    response.headers["Location"] = status_url
    return SubmitResponse(job_id=job.id, status=job.status, status_url=status_url)
