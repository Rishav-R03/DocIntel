"""Typed domain errors. Each carries a stable machine-readable ``code`` that clients can switch on
(implementation.md §12.2); the HTTP layer renders them as RFC 9457 problem+json.
"""

from enum import StrEnum
from http import HTTPStatus
from typing import ClassVar


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    # ingestion
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    SCHEMA_NOT_FOUND = "SCHEMA_NOT_FOUND"
    PDF_ENCRYPTED = "PDF_ENCRYPTED"
    PAGE_LIMIT_EXCEEDED = "PAGE_LIMIT_EXCEEDED"
    IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"
    DOCUMENT_UNREADABLE = "DOCUMENT_UNREADABLE"
    # extraction
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    EXTRACTION_TIMEOUT = "EXTRACTION_TIMEOUT"
    SYNC_CAPACITY_EXHAUSTED = "SYNC_CAPACITY_EXHAUSTED"
    # jobs / infrastructure
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


class DocIntelError(Exception):
    """Base class for errors that map to a documented API error code."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    title: str = "Internal error"
    headers: ClassVar[dict[str, str] | None] = None

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail


class InvalidRequestError(DocIntelError):
    code = ErrorCode.INVALID_REQUEST
    status_code = HTTPStatus.BAD_REQUEST
    title = "Invalid request"


class NotImplementedYetError(DocIntelError):
    code = ErrorCode.NOT_IMPLEMENTED
    status_code = HTTPStatus.NOT_IMPLEMENTED
    title = "Not implemented"


class PayloadTooLargeError(DocIntelError):
    code = ErrorCode.PAYLOAD_TOO_LARGE
    status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    title = "Payload too large"
    # we stop reading the body, so the connection can't be reused
    headers: ClassVar[dict[str, str] | None] = {"Connection": "close"}


class UnsupportedMediaTypeError(DocIntelError):
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE
    status_code = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    title = "Unsupported media type"


class SchemaNotFoundError(DocIntelError):
    code = ErrorCode.SCHEMA_NOT_FOUND
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Unknown schema"


class PdfEncryptedError(DocIntelError):
    code = ErrorCode.PDF_ENCRYPTED
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Encrypted PDF"


class PageLimitExceededError(DocIntelError):
    code = ErrorCode.PAGE_LIMIT_EXCEEDED
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Too many pages"


class ImageTooLargeError(DocIntelError):
    code = ErrorCode.IMAGE_TOO_LARGE
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Image dimensions too large"


class DocumentUnreadableError(DocIntelError):
    code = ErrorCode.DOCUMENT_UNREADABLE
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Document could not be read"


class JobNotFoundError(DocIntelError):
    code = ErrorCode.JOB_NOT_FOUND
    status_code = HTTPStatus.NOT_FOUND
    title = "Job not found"


class DependencyUnavailableError(DocIntelError):
    """A backing service (database, object storage, queue) failed. Safe for the client to retry."""

    code = ErrorCode.DEPENDENCY_UNAVAILABLE
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    title = "Dependency unavailable"
    headers: ClassVar[dict[str, str] | None] = {"Retry-After": "5"}


# ----------------------------------------------------------------------------- extraction
class ExtractionFailedError(DocIntelError):
    """The model call failed in a way retrying won't fix (bad request, auth, model error)."""

    code = ErrorCode.EXTRACTION_FAILED
    status_code = HTTPStatus.BAD_GATEWAY
    title = "Extraction failed"


class SchemaValidationFailedError(DocIntelError):
    code = ErrorCode.SCHEMA_VALIDATION_FAILED
    status_code = HTTPStatus.BAD_GATEWAY
    title = "Model output did not match the schema"


class OutputTruncatedError(DocIntelError):
    code = ErrorCode.OUTPUT_TRUNCATED
    status_code = HTTPStatus.BAD_GATEWAY
    title = "Model output was truncated"


class UpstreamUnavailableError(DocIntelError):
    code = ErrorCode.UPSTREAM_UNAVAILABLE
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    title = "AI provider unavailable"
    headers: ClassVar[dict[str, str] | None] = {"Retry-After": "10"}


class ExtractionTimeoutError(DocIntelError):
    code = ErrorCode.EXTRACTION_TIMEOUT
    status_code = HTTPStatus.GATEWAY_TIMEOUT
    title = "Extraction timed out"


class SyncCapacityExhaustedError(DocIntelError):
    code = ErrorCode.SYNC_CAPACITY_EXHAUSTED
    status_code = HTTPStatus.TOO_MANY_REQUESTS
    title = "Synchronous capacity exhausted"
    headers: ClassVar[dict[str, str] | None] = {"Retry-After": "2"}
