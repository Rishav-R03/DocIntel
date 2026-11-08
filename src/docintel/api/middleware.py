"""Request context middleware: request-id propagation, access logging, last-resort error handling.

Written as raw ASGI rather than ``BaseHTTPMiddleware``: that helper runs the downstream app in a
separate task and re-wraps every response body in a memory stream, which adds per-request overhead,
stops contextvar changes made downstream from flowing back, and puts an extra layer in front of
streaming (SSE) responses. Raw ASGI just observes ``send`` messages as they pass through.
"""

import re
import time
import uuid
from http import HTTPStatus

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from docintel.api.errors import error_response, problem_response
from docintel.domain.errors import (
    DocIntelError,
    ErrorCode,
    InvalidRequestError,
    PayloadTooLargeError,
)

REQUEST_ID_HEADER = "X-Request-Id"
CLIENT_ID_HEADER = "X-Client-Id"

# Inbound IDs are echoed into logs and headers, so only accept a conservative charset.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_QUIET_PATH_PREFIX = "/health/"

log = structlog.get_logger("docintel.access")


def safe_id(value: str | None) -> str | None:
    return value if value and _SAFE_ID.fullmatch(value) else None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = safe_id(headers.get(REQUEST_ID_HEADER)) or uuid.uuid4().hex
        client_id = safe_id(headers.get(CLIENT_ID_HEADER))
        path: str = scope["path"]
        start = time.perf_counter()
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id, client_id=client_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                log.exception("unhandled_exception", method=scope["method"], path=path)
                if response_started:
                    raise  # headers already sent; the server will drop the connection
                response = problem_response(
                    code=ErrorCode.INTERNAL_ERROR,
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    title="Internal error",
                    detail="An unexpected error occurred.",
                    instance=path,
                )
                await response(scope, receive, send_with_request_id)
            finally:
                log_method = log.debug if path.startswith(_QUIET_PATH_PREFIX) else log.info
                log_method(
                    "http_request",
                    method=scope["method"],
                    path=path,  # never the query string: it may carry client data
                    status=status_code,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                )


class BodySizeLimitMiddleware:
    """Coarse request-body cap for every route (the upload parser enforces the exact file limit).

    Rejects up front when ``Content-Length`` already exceeds the cap, and counts bytes as they are
    received for chunked bodies, raising mid-stream so the endpoint stops reading immediately.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            rejection: DocIntelError | None = None
            if not declared.isdigit():
                rejection = InvalidRequestError("Invalid Content-Length header.")
            elif int(declared) > self.max_body_bytes:
                rejection = PayloadTooLargeError("The request body is too large.")
            if rejection is not None:
                response = error_response(rejection, instance=scope["path"])
                await response(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise PayloadTooLargeError("The request body is too large.")
            return message

        await self.app(scope, limited_receive, send)
