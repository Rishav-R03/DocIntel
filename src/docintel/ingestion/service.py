"""Async job submission: inspect -> stage to S3 -> persist job + events -> enqueue.

The three writes (S3, Postgres, SQS) cannot share a transaction, so each failure point is handled
explicitly and always leaves a consistent, explainable state:

* S3 put fails       -> nothing persisted; 503.
* DB commit fails    -> best-effort delete of the staged object (lifecycle rule is the
                        backstop); 503.
* SQS publish fails  -> job marked FAILED(ENQUEUE_FAILED) so it can't sit in STAGED forever; 503.
* process dies between commit and publish -> job stays STAGED with no message; a stuck-job reaper
  (Phase 6) re-enqueues STAGED jobs older than a threshold. A transactional outbox would close this
  window fully but needs a relay process; not worth it at v1 scale.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import aiohttp
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.domain.errors import DependencyUnavailableError
from docintel.domain.ids import uuid7
from docintel.domain.job import EventStage, JobMode, JobStatus
from docintel.domain.messages import JobSubmitted
from docintel.ingestion.inspect import inspect_document
from docintel.ingestion.staging import DocumentStore, source_key
from docintel.ingestion.upload import UploadedDocument
from docintel.messaging import JobQueue
from docintel.persistence.errors import database_errors_as_unavailable
from docintel.persistence.models import Job
from docintel.persistence.repositories import EventRepository, JobRepository
from docintel.schemas.registry import SchemaSpec
from docintel.settings import Settings

log = structlog.get_logger(__name__)

AWS_ERRORS = (BotoCoreError, ClientError, aiohttp.ClientError, OSError, TimeoutError)


def _now() -> datetime:
    return datetime.now(UTC)


class IngestionService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        store: DocumentStore,
        queue: JobQueue,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._store = store
        self._queue = queue

    async def submit_async(
        self,
        upload: UploadedDocument,
        schema: SchemaSpec,
        *,
        request_id: str,
        client_id: str | None,
    ) -> Job:
        uploaded_at = _now()
        info = await asyncio.to_thread(
            inspect_document,
            upload.file,
            upload.mime_type,
            max_pages=min(self._settings.max_pages, schema.max_pages),
            max_image_pixels=self._settings.max_image_pixels,
        )
        job_id = uuid7()
        key = source_key(job_id, upload.mime_type, uploaded_at)

        with structlog.contextvars.bound_contextvars(job_id=str(job_id)):
            if upload.declared_content_type not in (None, upload.mime_type):
                log.info(
                    "content_type_mismatch",
                    declared=upload.declared_content_type,
                    sniffed=upload.mime_type,
                )
            await self._stage(key, upload, job_id=job_id, schema_id=schema.schema_id)
            staged_at = _now()

            job = Job(
                id=job_id,
                mode=JobMode.ASYNC,
                status=JobStatus.STAGED,
                schema_id=schema.schema_id,
                client_id=client_id,
                request_id=request_id,
                file_name=upload.filename,
                mime_type=upload.mime_type,
                size_bytes=upload.size_bytes,
                page_count=info.page_count,
                payload_sha256=upload.sha256,
                s3_key=key,
                attempt_count=0,
            )
            await self._persist(job, uploaded_at=uploaded_at, staged_at=staged_at)
            await self._enqueue(job, request_id=request_id)

            log.info(
                "job_submitted",
                schema_id=schema.schema_id,
                mime_type=upload.mime_type,
                size_bytes=upload.size_bytes,
                page_count=info.page_count,
            )
            return job

    async def _stage(
        self, key: str, upload: UploadedDocument, *, job_id: uuid.UUID, schema_id: str
    ) -> None:
        try:
            await self._store.put_source(
                key=key,
                body=upload.file,
                size_bytes=upload.size_bytes,
                mime_type=upload.mime_type,
                sha256_hex=upload.sha256,
                tags={"job_id": str(job_id), "schema_id": schema_id},
            )
        except AWS_ERRORS as exc:
            log.error("staging_failed", exc_info=True)
            raise DependencyUnavailableError(
                "Document storage is temporarily unavailable."
            ) from exc

    async def _persist(self, job: Job, *, uploaded_at: datetime, staged_at: datetime) -> None:
        try:
            with database_errors_as_unavailable():
                async with self._session_factory() as session, session.begin():
                    JobRepository(session).add(job)
                    events = EventRepository(session)
                    events.add(
                        job.id,
                        EventStage.UPLOADED,
                        at=uploaded_at,
                        payload={"size_bytes": job.size_bytes, "mime_type": job.mime_type},
                    )
                    events.add(job.id, EventStage.S3_STAGED, at=staged_at)
        except DependencyUnavailableError:
            log.error("job_persist_failed", exc_info=True)
            await self._discard_staged(job.s3_key)
            raise

    async def _enqueue(self, job: Job, *, request_id: str) -> None:
        try:
            await self._queue.publish(
                JobSubmitted(job_id=job.id, schema_id=job.schema_id), request_id=request_id
            )
        except AWS_ERRORS as exc:
            log.error("enqueue_failed", exc_info=True)
            await self._mark_failed(job, error_code="ENQUEUE_FAILED")
            raise DependencyUnavailableError("The job could not be queued; please retry.") from exc

    async def _mark_failed(self, job: Job, *, error_code: str) -> None:
        now = _now()
        try:
            async with self._session_factory() as session, session.begin():
                updated = await JobRepository(session).transition(
                    job.id,
                    from_states=[JobStatus.STAGED],
                    to_state=JobStatus.FAILED,
                    error_code=error_code,
                    completed_at=now,
                )
                if updated is not None:
                    EventRepository(session).add(
                        job.id, EventStage.FAILED, at=now, payload={"error_code": error_code}
                    )
            job.status = JobStatus.FAILED
        except Exception:
            log.error("mark_failed_failed", exc_info=True)  # reaper picks it up (Phase 6)

    async def _discard_staged(self, key: str) -> None:
        try:
            await self._store.delete(key)
        except Exception:
            log.warning("staged_object_cleanup_failed", key=key, exc_info=True)
