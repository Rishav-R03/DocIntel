"""Process-wide resources (DB engine, AWS clients, Gemini client, services) owned by the FastAPI
lifespan.

``create_app`` takes the factory as a parameter so tests can substitute fakes without
patching module globals.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import structlog
from google import genai
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.aws import client_kwargs, create_session
from docintel.extraction.factory import build_pipeline
from docintel.extraction.gemini_client import create_genai_client
from docintel.extraction.pipeline import ExtractionPipeline
from docintel.health import DatabaseCheck, EventListenerCheck, HealthCheck, S3BucketCheck
from docintel.ingestion.service import IngestionService
from docintel.ingestion.staging import DocumentStore
from docintel.messaging import JobQueue
from docintel.persistence.db import create_engine, create_session_factory
from docintel.persistence.events import EventBroker
from docintel.settings import Settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Resources:
    health_checks: tuple[HealthCheck, ...]
    session_factory: async_sessionmaker[AsyncSession]
    ingestion: IngestionService
    events: EventBroker


ResourcesFactory = Callable[[Settings], AbstractAsyncContextManager[Resources]]


@asynccontextmanager
async def build_resources(
    settings: Settings, *, pipeline: ExtractionPipeline | None = None
) -> AsyncIterator[Resources]:
    """`pipeline` overrides the Gemini-backed pipeline (tests inject a scripted fake)."""
    engine = create_engine(settings, role="api")
    session_factory = create_session_factory(engine)
    aws = create_session(settings)
    gemini: genai.Client | None = None
    if pipeline is None and settings.gemini_api_key is not None:
        gemini = create_genai_client(settings.gemini_api_key.get_secret_value())
        pipeline = build_pipeline(settings, gemini)
    elif pipeline is None:
        log.warning("gemini_not_configured", detail="mode=sync will return 503")
    try:
        async with (
            aws.client("s3", **client_kwargs(settings)) as s3,
            aws.client("sqs", **client_kwargs(settings)) as sqs,
        ):
            store = DocumentStore(s3, bucket=settings.s3_bucket, kms_key_id=settings.s3_kms_key_id)
            ingestion = IngestionService(
                settings=settings,
                session_factory=session_factory,
                store=store,
                queue=JobQueue(sqs, settings.sqs_jobs_url),
                pipeline=pipeline,
            )
            events = EventBroker(settings)
            await events.start()
            try:
                yield Resources(
                    health_checks=(
                        DatabaseCheck(engine),
                        S3BucketCheck(s3, settings.s3_bucket),
                        EventListenerCheck(events),
                    ),
                    session_factory=session_factory,
                    ingestion=ingestion,
                    events=events,
                )
            finally:
                await events.stop()
                await ingestion.drain()  # before clients close: persistence still needs them
    finally:
        if gemini is not None:
            await gemini.aio.aclose()
        await engine.dispose()
