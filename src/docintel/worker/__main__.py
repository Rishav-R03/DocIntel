"""Worker entrypoint: ``python -m docintel.worker``.

ECS sends SIGTERM, waits ``stopTimeout`` (30 s in the task definition), then SIGKILLs. The drain
timeout (25 s default) is set below that so in-flight jobs finish or are released cleanly first.
"""

import asyncio
import signal
from collections.abc import Callable

import structlog

from docintel.aws import client_kwargs, create_session
from docintel.extraction.factory import build_pipeline
from docintel.extraction.gemini_client import create_genai_client
from docintel.ingestion.staging import DocumentStore
from docintel.logging import configure_logging
from docintel.persistence.db import create_engine, create_session_factory
from docintel.settings import get_settings
from docintel.worker.extraction_handler import ExtractionHandler
from docintel.worker.runner import Worker

log = structlog.get_logger(__name__)


def _install_signal_handlers(callback: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, callback)
        except NotImplementedError:  # Windows dev machines
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(callback))


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine(settings, role="worker")
    genai_client = (
        create_genai_client(settings.gemini_api_key.get_secret_value())
        if settings.gemini_api_key is not None
        else None
    )
    if genai_client is None:
        log.warning("gemini_not_configured", detail="jobs will retry, then fail")
    aws = create_session(settings)
    try:
        async with (
            aws.client("sqs", **client_kwargs(settings)) as sqs,
            aws.client("s3", **client_kwargs(settings)) as s3,
        ):
            handler = ExtractionHandler(
                settings=settings,
                session_factory=create_session_factory(engine),
                store=DocumentStore(
                    s3, bucket=settings.s3_bucket, kms_key_id=settings.s3_kms_key_id
                ),
                pipeline=(
                    build_pipeline(
                        settings, genai_client, max_attempts=settings.retry_max_attempts_async
                    )
                    if genai_client is not None
                    else None
                ),
            )
            worker = Worker(
                sqs=sqs,
                queue_url=settings.sqs_jobs_url,
                handler=handler,
                concurrency=settings.worker_concurrency,
                wait_time_s=settings.sqs_wait_time_s,
                drain_timeout_s=settings.worker_drain_timeout_s,
                visibility_timeout_s=settings.sqs_visibility_timeout_s,
            )
            _install_signal_handlers(worker.request_stop)
            await worker.run()
    finally:
        if genai_client is not None:
            await genai_client.aio.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
