"""Process-wide resources (DB engine, AWS clients, services) owned by the FastAPI lifespan.

``create_app`` takes the factory as a parameter so tests can substitute fakes without
patching module globals.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.aws import client_kwargs, create_session
from docintel.health import DatabaseCheck, HealthCheck, S3BucketCheck
from docintel.ingestion.service import IngestionService
from docintel.ingestion.staging import DocumentStore
from docintel.messaging import JobQueue
from docintel.persistence.db import create_engine, create_session_factory
from docintel.settings import Settings


@dataclass(frozen=True)
class Resources:
    health_checks: tuple[HealthCheck, ...]
    session_factory: async_sessionmaker[AsyncSession]
    ingestion: IngestionService


ResourcesFactory = Callable[[Settings], AbstractAsyncContextManager[Resources]]


@asynccontextmanager
async def build_resources(settings: Settings) -> AsyncIterator[Resources]:
    engine = create_engine(settings, role="api")
    session_factory = create_session_factory(engine)
    aws = create_session(settings)
    try:
        async with (
            aws.client("s3", **client_kwargs(settings)) as s3,
            aws.client("sqs", **client_kwargs(settings)) as sqs,
        ):
            store = DocumentStore(s3, bucket=settings.s3_bucket, kms_key_id=settings.s3_kms_key_id)
            yield Resources(
                health_checks=(DatabaseCheck(engine), S3BucketCheck(s3, settings.s3_bucket)),
                session_factory=session_factory,
                ingestion=IngestionService(
                    settings=settings,
                    session_factory=session_factory,
                    store=store,
                    queue=JobQueue(sqs, settings.sqs_jobs_url),
                ),
            )
    finally:
        await engine.dispose()
