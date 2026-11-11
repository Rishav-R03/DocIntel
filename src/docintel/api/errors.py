"""RFC 9457 ``application/problem+json`` rendering for every error the API returns."""

from http import HTTPStatus
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from docintel.domain.errors import DocIntelError, ErrorCode

PROBLEM_MEDIA_TYPE = "application/problem+json"

_STATUS_TO_CODE = {
    HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND,
    HTTPStatus.METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
}


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: ErrorCode
    request_id: str | None = None
    errors: list[dict[str, Any]] | None = None


def problem_response(
    *,
    code: ErrorCode,
    status: int,
    title: str,
    detail: str | None,
    instance: str | None,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    problem = Problem(
        type=f"urn:docintel:error:{code.lower()}",
        title=title,
        status=status,
        detail=detail,
        instance=instance,
        code=code,
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        errors=errors,
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True), status_code=status, media_type=PROBLEM_MEDIA_TYPE
    )


def error_response(err: DocIntelError, *, instance: str | None) -> JSONResponse:
    response = problem_response(
        code=err.code,
        status=err.status_code,
        title=err.title,
        detail=err.detail,
        instance=instance,
    )
    if err.headers:
        response.headers.update(err.headers)
    return response


async def _docintel_error(request: Request, exc: Exception) -> JSONResponse:
    return error_response(cast(DocIntelError, exc), instance=request.url.path)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in cast(RequestValidationError, exc).errors()
    ]
    return problem_response(
        code=ErrorCode.INVALID_REQUEST,
        status=HTTPStatus.BAD_REQUEST,
        title="Invalid request",
        detail="The request failed validation.",
        instance=request.url.path,
        errors=errors,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)
    status = HTTPStatus(http_exc.status_code)
    response = problem_response(
        code=_STATUS_TO_CODE.get(status, ErrorCode.HTTP_ERROR),
        status=http_exc.status_code,
        title=status.phrase,
        detail=http_exc.detail if isinstance(http_exc.detail, str) else None,
        instance=request.url.path,
    )
    if http_exc.headers:
        response.headers.update(http_exc.headers)
    return response


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries for error statuses, all rendered as problem+json."""
    return {s: {"model": Problem, "description": HTTPStatus(s).phrase} for s in statuses}


def install_problem_openapi(app: FastAPI) -> None:
    """Make the published contract match runtime behaviour.

    FastAPI documents request-validation failures as ``422 HTTPValidationError`` with
    ``application/json``; this service returns ``400`` problem+json instead. Rewrite the generated
    schema so every error response points at ``Problem`` under ``application/problem+json``.
    """

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        problem_ref = {"$ref": "#/components/schemas/Problem"}
        for operations in schema.get("paths", {}).values():
            for operation in operations.values():
                responses = operation.get("responses", {})
                auto_422 = responses.get("422", {}).get("content", {}).get("application/json", {})
                if auto_422.get("schema", {}).get("$ref", "").endswith("/HTTPValidationError"):
                    del responses["422"]
                    responses.setdefault("400", {"description": "Bad Request"})
                for status, spec in responses.items():
                    if int(status) >= 400:
                        spec["content"] = {PROBLEM_MEDIA_TYPE: {"schema": problem_ref}}
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.pop("HTTPValidationError", None)
        components.pop("ValidationError", None)
        if "Problem" not in components:  # FastAPI adds it (and ErrorCode) via problem_responses
            components["Problem"] = Problem.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def register_exception_handlers(app: FastAPI) -> None:
    # Unhandled exceptions are handled in RequestContextMiddleware, not here: an ``Exception``
    # handler would run in Starlette's outermost ServerErrorMiddleware, outside our request
    # context, so the response would lose its X-Request-Id and the log line its request_id.
    app.add_exception_handler(DocIntelError, _docintel_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
