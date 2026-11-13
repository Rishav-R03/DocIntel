"""Data access for jobs and job events. Repositories never commit: the caller owns the
transaction boundary, so several writes can be atomic."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.domain.job import EventStage, JobStatus, check_transition
from docintel.persistence.models import ExtractionResult, Job, JobEvent, LlmCallAudit


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, job: Job) -> None:
        self._session.add(job)

    async def get(self, job_id: uuid.UUID) -> Job | None:
        return await self._session.get(Job, job_id)

    async def transition(
        self,
        job_id: uuid.UUID,
        *,
        from_states: Collection[JobStatus],
        to_state: JobStatus,
        **values: Any,
    ) -> Job | None:
        """Compare-and-set on ``status``: ``UPDATE ... WHERE id = :id AND status IN (:from)``.

        Returns the updated job, or None when the job is missing or no longer in ``from_states``
        (e.g. another worker already claimed it). This single conditional statement is what makes
        at-least-once message delivery safe without explicit row locks.
        """
        check_transition(from_states, to_state)
        stmt = (
            update(Job)
            .where(Job.id == job_id, Job.status.in_(list(from_states)))
            .values(status=to_state, **values)
            .returning(Job)
            .execution_options(synchronize_session=False)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class ResultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, result: ExtractionResult) -> None:
        self._session.add(result)

    async def get(self, job_id: uuid.UUID) -> ExtractionResult | None:
        return await self._session.get(ExtractionResult, job_id)


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_all(self, rows: Sequence[LlmCallAudit]) -> None:
        self._session.add_all(rows)

    async def usage(self, job_id: uuid.UUID) -> "UsageSummary":
        """Totals across *all* attempts (failed and repaired ones are billed too)."""
        row = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(LlmCallAudit.prompt_tokens), 0),
                    func.coalesce(func.sum(LlmCallAudit.output_tokens), 0),
                    func.coalesce(func.sum(LlmCallAudit.thinking_tokens), 0),
                    func.coalesce(func.sum(LlmCallAudit.estimated_cost_usd), 0),
                    func.coalesce(func.sum(LlmCallAudit.latency_ms), 0),
                ).where(LlmCallAudit.job_id == job_id)
            )
        ).one()
        last_success = await self._session.scalar(
            select(LlmCallAudit.model)
            .where(LlmCallAudit.job_id == job_id, LlmCallAudit.outcome == "SUCCESS")
            .order_by(LlmCallAudit.id.desc())
            .limit(1)
        )
        return UsageSummary(
            input_tokens=int(row[0] or 0),
            output_tokens=int(row[1] or 0),
            thinking_tokens=int(row[2] or 0),
            cost_usd=Decimal(row[3] or 0),
            model_ms=int(row[4] or 0),
            model=last_success,
        )


@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cost_usd: Decimal
    model_ms: int
    model: str | None


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(
        self,
        job_id: uuid.UUID,
        stage: EventStage,
        *,
        at: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> JobEvent:
        event = JobEvent(job_id=job_id, stage=stage.value, payload=payload or {})
        if at is not None:  # record when the stage actually happened, not when it was persisted
            event.created_at = at
        self._session.add(event)
        return event

    async def list_for_job(self, job_id: uuid.UUID, *, after_id: int = 0) -> Sequence[JobEvent]:
        stmt = (
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.id > after_id)
            .order_by(JobEvent.id)
        )
        return (await self._session.scalars(stmt)).all()
