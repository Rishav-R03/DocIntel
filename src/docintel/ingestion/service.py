"""Job submission in both modes.

**Sync** (``extract_sync``): inspect -> [S3 staging || Gemini extraction] -> respond -> persist.
Staging runs concurrently with the model call and persistence happens after the response, so
neither S3 nor Postgres latency sits on the critical path, and neither outage fails the request
(degradation matrix, implementation.md §4.10.4). If persistence fails, the full audit record is
emitted as a structured ``audit_fallback`` log line (Phase 5 adds automated replay).

Source retention: on COMPLETED the staged object is deleted (the data is no longer needed); on
FAILED it is kept so the job can be re-driven, and the 1-day lifecycle rule removes it.

**Async** (``submit_async``): inspect -> stage to S3 -> persist job + events -> enqueue.

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
import dataclasses
import io
import time
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.domain.errors import (
    DependencyUnavailableError,
    DocIntelError,
    SyncCapacityExhaustedError,
    UpstreamUnavailableError,
)
from docintel.domain.ids import uuid7
from docintel.domain.job import EventStage, JobMode, JobStatus
from docintel.domain.messages import JobSubmitted
from docintel.extraction.pipeline import Attempt, ExtractionOutcome, ExtractionPipeline
from docintel.ingestion.inspect import DocumentInfo, inspect_document
from docintel.ingestion.staging import DocumentStore, source_key
from docintel.ingestion.upload import UploadedDocument
from docintel.messaging import JobQueue
from docintel.persistence.errors import database_errors_as_unavailable
from docintel.persistence.models import Job
from docintel.persistence.records import audit_row, jsonable, result_row
from docintel.persistence.repositories import (
    AuditRepository,
    EventRepository,
    JobRepository,
    ResultRepository,
)
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
        pipeline: ExtractionPipeline | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._store = store
        self._queue = queue
        self._pipeline = pipeline  # None when no Gemini key is configured (local dev only)
        self._sync_slots = asyncio.Semaphore(settings.sync_max_concurrency)
        self._background: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ sync mode
    async def extract_sync(
        self,
        upload: UploadedDocument,
        schema: SchemaSpec,
        *,
        request_id: str,
        client_id: str | None,
    ) -> tuple[Job, ExtractionOutcome]:
        if self._pipeline is None:
            raise UpstreamUnavailableError("Extraction is not configured on this instance.")
        # Fail fast instead of queueing: a sync caller waiting behind 32 others would blow its own
        # deadline anyway. No await between the check and the acquire, so this can't race.
        if self._sync_slots.locked():
            raise SyncCapacityExhaustedError(
                "All synchronous extraction slots are busy; retry shortly or use mode=async."
            )
        async with self._sync_slots:
            uploaded_at = _now()
            deadline = time.monotonic() + self._settings.sync_deadline_s
            info = await self._inspect(upload, schema)
            document = upload.file.read()
            job_id = uuid7()
            job = self._new_job(
                job_id,
                upload,
                schema,
                info,
                mode=JobMode.SYNC,
                status=JobStatus.PROCESSING,
                request_id=request_id,
                client_id=client_id,
                at=uploaded_at,
            )
            with structlog.contextvars.bound_contextvars(job_id=str(job_id)):
                stage = asyncio.create_task(self._stage_best_effort(job, document))
                attempts: list[Attempt] = []
                started_at = _now()
                try:
                    outcome = await self._pipeline.run(
                        document=document,
                        mime_type=upload.mime_type,
                        spec=schema,
                        deadline=deadline,
                        attempts=attempts,
                    )
                except DocIntelError as exc:
                    self._spawn(
                        self._persist_sync(job, attempts, None, exc, stage, uploaded_at, started_at)
                    )
                    raise
                self._spawn(
                    self._persist_sync(job, attempts, outcome, None, stage, uploaded_at, started_at)
                )
                log.info(
                    "sync_extraction_completed",
                    schema_id=schema.schema_id,
                    validity=outcome.validity.adjusted,
                    attempts=len(attempts),
                    gemini_ms=outcome.gemini_ms,
                    cost_usd=str(outcome.cost_usd),
                )
                return job, outcome

    async def drain(self, timeout_s: float = 10.0) -> None:
        """Await post-response persistence on shutdown so audit rows aren't lost on deploys."""
        if self._background:
            await asyncio.wait(set(self._background), timeout=timeout_s)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)  # keep a strong reference until done
        task.add_done_callback(self._background.discard)

    async def _stage_best_effort(self, job: Job, document: bytes) -> bool:
        try:
            await self._store.put_source(
                key=job.s3_key,
                body=io.BytesIO(document),
                size_bytes=job.size_bytes,
                mime_type=job.mime_type,
                sha256_hex=job.payload_sha256,
                tags={"job_id": str(job.id), "schema_id": job.schema_id},
            )
        except AWS_ERRORS:
            log.warning("sync_staging_failed", exc_info=True)  # extraction proceeds regardless
            return False
        return True

    async def _persist_sync(
        self,
        job: Job,
        attempts: list[Attempt],
        outcome: ExtractionOutcome | None,
        error: DocIntelError | None,
        stage: "asyncio.Task[bool]",
        uploaded_at: datetime,
        started_at: datetime,
    ) -> None:
        staged = await stage
        finished_at = _now()
        job.status = JobStatus.COMPLETED if outcome else JobStatus.FAILED
        job.started_at = started_at
        job.completed_at = finished_at
        job.duration_ms = int((finished_at - uploaded_at).total_seconds() * 1000)
        job.attempt_count = len(attempts)
        if error is not None:
            job.error_code = error.code.value
            job.error_detail = error.detail
        try:
            with database_errors_as_unavailable():
                async with self._session_factory() as session, session.begin():
                    JobRepository(session).add(job)
                    await session.flush()  # job row before its children (FKs)
                    events = EventRepository(session)
                    events.add(
                        job.id,
                        EventStage.UPLOADED,
                        at=uploaded_at,
                        payload={"size_bytes": job.size_bytes, "mime_type": job.mime_type},
                    )
                    if staged:
                        events.add(job.id, EventStage.S3_STAGED, at=started_at)
                    events.add(job.id, EventStage.GEMINI_PROCESSING, at=started_at)
                    if outcome is not None:
                        events.add(job.id, EventStage.VALIDATED, at=finished_at)
                        events.add(job.id, EventStage.COMPLETED, at=finished_at)
                        ResultRepository(session).add(result_row(job, outcome))
                    else:
                        events.add(
                            job.id,
                            EventStage.FAILED,
                            at=finished_at,
                            payload={"error_code": job.error_code},
                        )
                    AuditRepository(session).add_all([audit_row(job.id, a) for a in attempts])
        except DependencyUnavailableError:
            # Graceful degradation: the client already has its answer. Keep the audit trail in
            # the log stream (no document content, only metadata/tokens/cost) for replay.
            log.error(
                "sync_persist_failed",
                audit_fallback=True,
                job={
                    "id": str(job.id),
                    "status": job.status.value,
                    "schema_id": job.schema_id,
                    "error_code": job.error_code,
                    "duration_ms": job.duration_ms,
                },
                attempts=[jsonable(dataclasses.asdict(a)) for a in attempts],
                exc_info=True,
            )
            return
        if outcome is not None and staged:
            await self._discard_staged(job.s3_key)  # COMPLETED: source no longer needed

    def _new_job(
        self,
        job_id: uuid.UUID,
        upload: UploadedDocument,
        schema: SchemaSpec,
        info: DocumentInfo,
        *,
        mode: JobMode,
        status: JobStatus,
        request_id: str,
        client_id: str | None,
        at: datetime,
    ) -> Job:
        if upload.declared_content_type not in (None, upload.mime_type):
            log.info(
                "content_type_mismatch",
                declared=upload.declared_content_type,
                sniffed=upload.mime_type,
            )
        return Job(
            id=job_id,
            mode=mode,
            status=status,
            schema_id=schema.schema_id,
            client_id=client_id,
            request_id=request_id,
            file_name=upload.filename,
            mime_type=upload.mime_type,
            size_bytes=upload.size_bytes,
            page_count=info.page_count,
            payload_sha256=upload.sha256,
            s3_key=source_key(job_id, upload.mime_type, at),
            attempt_count=0,
        )

    async def _inspect(self, upload: UploadedDocument, schema: SchemaSpec) -> DocumentInfo:
        return await asyncio.to_thread(
            inspect_document,
            upload.file,
            upload.mime_type,
            max_pages=min(self._settings.max_pages, schema.max_pages),
            max_image_pixels=self._settings.max_image_pixels,
        )

    # ------------------------------------------------------------------ async mode

    async def submit_async(
        self,
        upload: UploadedDocument,
        schema: SchemaSpec,
        *,
        request_id: str,
        client_id: str | None,
    ) -> Job:
        uploaded_at = _now()
        info = await self._inspect(upload, schema)
        job_id = uuid7()
        with structlog.contextvars.bound_contextvars(job_id=str(job_id)):
            job = self._new_job(
                job_id,
                upload,
                schema,
                info,
                mode=JobMode.ASYNC,
                status=JobStatus.STAGED,
                request_id=request_id,
                client_id=client_id,
                at=uploaded_at,
            )
            await self._stage(job.s3_key, upload, job_id=job_id, schema_id=schema.schema_id)
            staged_at = _now()
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
