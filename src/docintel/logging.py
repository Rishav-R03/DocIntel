"""Structured logging: structlog for our code, stdlib (uvicorn, botocore, alembic) routed through
the same processor chain so every line on stdout is one JSON object with the same keys.

Request/job correlation uses ``structlog.contextvars``: bind once at the edge (middleware,
worker message loop) and every log call below inherits ``request_id`` / ``job_id``.
"""

import logging
import sys

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from docintel.settings import Settings

# Structured tracebacks WITHOUT frame locals. structlog's default ``dict_tracebacks`` serialises
# every frame's local variables, which put the database password (asyncpg connect frames) and
# whole job/attempt objects into the log stream. Frames (file/line/function) are enough to debug.
_STRUCTURED_TRACEBACKS = structlog.processors.ExceptionRenderer(
    structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
)

# HTTP client internals log every connect/TLS/header step at DEBUG, burying useful lines.
_NOISY_LOGGERS = (
    "botocore",
    "aiobotocore",
    "boto3",
    "urllib3",
    "asyncio",
    "httpcore",
    "httpx",
    "httpx2",
    "hpack",
    "google_genai",
)


def _static_fields(settings: Settings) -> Processor:
    fields = {"service": settings.service_name, "env": settings.env, "version": settings.version}

    def add_static_fields(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        for key, value in fields.items():
            event_dict.setdefault(key, value)
        return event_dict

    return add_static_fields


def configure_logging(settings: Settings) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _static_fields(settings),
    ]
    renderer: list[Processor] = (
        [_STRUCTURED_TRACEBACKS, structlog.processors.JSONRenderer()]
        if settings.log_json
        else [structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)]
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # uvicorn installs its own handlers; strip them so its records flow through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # pypdf warns on every recoverable oddity in client-supplied PDFs; those are expected noise.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
