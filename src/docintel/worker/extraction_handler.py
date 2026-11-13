"""Async job processing: SQS message -> claim -> extract -> persist -> notify (Phase 4).

Delivery is at-least-once, so every step is safe to repeat:

* **Claim** is a compare-and-set ``STAGED -> PROCESSING``. A duplicate delivery finds nothing to
  claim and is acked. A *redelivered* message (receive count > 1) may also reclaim a job stuck in
  ``PROCESSING``: the runner's visibility heartbeat stops only when a worker dies, so a redelivery
  while PROCESSING means the previous owner is gone.
* **Transient failures** (provider overload, deadline, DB/S3 hiccups) put the job back to
  ``STAGED`` and reschedule the message with backoff. On the last allowed receive the job is marked
  ``FAILED`` instead, so its state never silently diverges from the DLQ.
* **Permanent failures** (bad output, rejected request, missing source) mark ``FAILED`` and ack.

Every Gemini attempt is written to ``llm_call_audit`` on every path, success or not.
"""

import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import structlog
from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.domain.errors import (
    DependencyUnavailableError,
    DocIntelError,
    ExtractionFailedError,
    ExtractionTimeoutError,
    UpstreamUnavailableError,
)
from docintel.domain.job import EventStage, JobStatus
from docintel.domain.messages import JobSubmitted
from docintel.extraction.pipeline import Attempt, ExtractionOutcome, ExtractionPipeline
from docintel.ingestion.service import AWS_ERRORS
from docintel.ingestion.staging import DocumentStore
from docintel.persistence.errors import database_errors_as_unavailable
from docintel.persistence.events import notify_job_event
from docintel.persistence.models import Job
from docintel.persistence.records import audit_row, result_row
from docintel.persistence.repositories import (
    AuditRepository,
    EventRepository,
    JobRepository,
    ResultRepository,
)
from docintel.schemas.registry import get_schema
from docintel.settings import Settings
from docintel.worker.handler import RetryLaterError

log = structlog.get_logger(__name__)

RETRYABLE = (UpstreamUnavailableError, ExtractionTimeoutError, DependencyUnavailableError)
_MAX_BACKOFF_S = 900


def _now() -> datetime:
    return datetime.now(UTC)


def retry_delay_s(receive_count: int) -> int:
    """30 s, 60 s, 120 s, ... capped at 15 min: gives an overloaded provider room to recover."""
    return int(min(_MAX_BACKOFF_S, 30 * 2 ** (receive_count - 1)))


class SourceMissingError(ExtractionFailedError):
    title = "Source document missing"


class ExtractionHandler:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        store: DocumentStore,
        pipeline: ExtractionPipeline | None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._store = store
        self._pipeline = pipeline

    async def handle(self, message: Mapping[str, Any]) -> None:
        # An unparseable body raises: after max receives SQS moves it to the DLQ, where it is kept
        # for inspection instead of being silently dropped.
        submitted = JobSubmitted.model_validate_json(message["Body"])
        receive_count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        with structlog.contextvars.bound_contextvars(job_id=str(submitted.job_id)):
            job = await self._claim(submitted.job_id, receive_count)
            if job is None:
                log.info("job_not_claimable")  # duplicate delivery or already terminal: ack
                return
            final_attempt = receive_count >= self._settings.worker_max_receives
            attempts: list[Attempt] = []
            try:
                if self._pipeline is None:
                    raise UpstreamUnavailableError("Extraction is not configured on this worker.")
                document = await self._load_source(job)
                outcome = await self._pipeline.run(
                    document=document,
                    mime_type=job.mime_type,
                    spec=get_schema(job.schema_id),
                    deadline=time.monotonic() + self._settings.worker_job_deadline_s,
                    attempts=attempts,
                )
            except DocIntelError as exc:
                retry = isinstance(exc, RETRYABLE) and not final_attempt
                await self._record_failure(
                    job, attempts, exc, retry=retry, receive_count=receive_count
                )
                if retry:
                    raise RetryLaterError(
                        exc.code.value, delay_s=retry_delay_s(receive_count)
                    ) from exc
                return
            await self._record_success(job, attempts, outcome)
            await self._discard_source(job)
            log.info(
                "async_extraction_completed",
                validity=outcome.validity.adjusted,
                attempts=len(attempts),
                cost_usd=str(outcome.cost_usd),
            )

    # ------------------------------------------------------------------ steps
    async def _claim(self, job_id: uuid.UUID, receive_count: int) -> Job | None:
        from_states = [JobStatus.STAGED]
        if receive_count > 1:
            from_states.append(JobStatus.PROCESSING)  # previous owner died mid-run
        now = _now()
        try:
            with database_errors_as_unavailable():
                async with self._session_factory() as session, session.begin():
                    job = await JobRepository(session).transition(
                        job_id,
                        from_states=from_states,
                        to_state=JobStatus.PROCESSING,
                        started_at=now,
                        attempt_count=Job.attempt_count + 1,
                    )
                    if job is not None:
                        await self._event(
                            session,
                            job.id,
                            EventStage.GEMINI_PROCESSING,
                            now,
                            {"receive_count": receive_count},
                        )
                    return job
        except DependencyUnavailableError as exc:
            raise RetryLaterError(
                "database unavailable", delay_s=retry_delay_s(receive_count)
            ) from exc

    async def _load_source(self, job: Job) -> bytes:
        try:
            return await self._store.get(job.s3_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise SourceMissingError(
                    "The staged document no longer exists (expired or deleted)."
                ) from exc
            raise DependencyUnavailableError("Document storage is unavailable.") from exc
        except AWS_ERRORS as exc:
            raise DependencyUnavailableError("Document storage is unavailable.") from exc

    async def _record_success(
        self, job: Job, attempts: list[Attempt], outcome: ExtractionOutcome
    ) -> None:
        now = _now()
        try:
            with database_errors_as_unavailable():
                async with self._session_factory() as session, session.begin():
                    jobs = JobRepository(session)
                    await jobs.transition(
                        job.id, from_states=[JobStatus.PROCESSING], to_state=JobStatus.VALIDATING
                    )
                    await jobs.transition(
                        job.id,
                        from_states=[JobStatus.VALIDATING],
                        to_state=JobStatus.COMPLETED,
                        completed_at=now,
                        duration_ms=_duration_ms(job, now),
                    )
                    ResultRepository(session).add(result_row(job, outcome))
                    AuditRepository(session).add_all([audit_row(job.id, a) for a in attempts])
                    await self._event(session, job.id, EventStage.VALIDATED, now)
                    await self._event(
                        session,
                        job.id,
                        EventStage.COMPLETED,
                        now,
                        {"document_validity_score": outcome.validity.adjusted},
                    )
        except DependencyUnavailableError as exc:
            # Result not saved: let the message come back. The job stays PROCESSING and the
            # redelivery reclaims it (re-extraction costs tokens; Phase 5 adds an S3 result copy).
            log.error("result_persist_failed", exc_info=True)
            raise RetryLaterError("database unavailable", delay_s=retry_delay_s(1)) from exc

    async def _record_failure(
        self,
        job: Job,
        attempts: list[Attempt],
        error: DocIntelError,
        *,
        retry: bool,
        receive_count: int,
    ) -> None:
        now = _now()
        try:
            with database_errors_as_unavailable():
                async with self._session_factory() as session, session.begin():
                    jobs = JobRepository(session)
                    if retry:
                        await jobs.transition(
                            job.id, from_states=[JobStatus.PROCESSING], to_state=JobStatus.STAGED
                        )
                        await self._event(
                            session,
                            job.id,
                            EventStage.RETRY_SCHEDULED,
                            now,
                            {
                                "error_code": error.code.value,
                                "retry_in_s": retry_delay_s(receive_count),
                            },
                        )
                    else:
                        await jobs.transition(
                            job.id,
                            from_states=[JobStatus.PROCESSING],
                            to_state=JobStatus.FAILED,
                            error_code=error.code.value,
                            error_detail=error.detail,
                            completed_at=now,
                            duration_ms=_duration_ms(job, now),
                        )
                        await self._event(
                            session,
                            job.id,
                            EventStage.FAILED,
                            now,
                            {"error_code": error.code.value},
                        )
                    AuditRepository(session).add_all([audit_row(job.id, a) for a in attempts])
        except DependencyUnavailableError as exc:
            log.error("failure_persist_failed", exc_info=True)
            raise RetryLaterError(
                "database unavailable", delay_s=retry_delay_s(receive_count)
            ) from exc
        log.warning(
            "async_extraction_failed",
            error_code=error.code.value,
            will_retry=retry,
            receive_count=receive_count,
        )

    async def _discard_source(self, job: Job) -> None:
        try:
            await self._store.delete(job.s3_key)  # COMPLETED: source no longer needed
        except Exception:
            log.warning("staged_object_cleanup_failed", exc_info=True)  # lifecycle rule backstop

    @staticmethod
    async def _event(
        session: AsyncSession,
        job_id: uuid.UUID,
        stage: EventStage,
        at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> None:
        EventRepository(session).add(job_id, stage, at=at, payload=payload)
        await session.flush()
        await notify_job_event(session, job_id)  # delivered on commit


def _duration_ms(job: Job, now: datetime) -> int:
    return int((now - job.created_at).total_seconds() * 1000)
