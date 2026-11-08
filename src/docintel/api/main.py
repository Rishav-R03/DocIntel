"""FastAPI application factory.

Run with: ``uvicorn --factory docintel.api.main:create_app``. A factory (instead of a module-level
``app``) means importing this module never reads the environment, so tests build apps with
explicit settings.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from docintel.api.errors import register_exception_handlers
from docintel.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from docintel.api.resources import ResourcesFactory, build_resources
from docintel.api.routes import extract, health, jobs
from docintel.ingestion.upload import MULTIPART_OVERHEAD_BYTES
from docintel.logging import configure_logging
from docintel.settings import Settings, get_settings

log = structlog.get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    resources_factory: ResourcesFactory = build_resources,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with resources_factory(settings) as resources:
            app.state.settings = settings
            app.state.resources = resources
            log.info("api_started")
            yield
            log.info("api_stopping")

    is_prod = settings.env == "prod"
    app = FastAPI(
        title="DocIntel AI Engine",
        version=settings.version,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
    )
    # Starlette wraps in reverse order of registration: RequestContextMiddleware is outermost, so
    # even a 413 from the body guard carries X-Request-Id and is access-logged.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES,
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(extract.router)
    app.include_router(jobs.router)
    return app
