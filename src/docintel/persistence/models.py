"""ORM models mirroring implementation.md §4.8.

Migrations are hand-written (``migrations/versions``); these models are the target metadata
Alembic compares against, and an integration test asserts the two never drift apart.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from docintel.domain.job import JobMode, JobStatus

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pg_enum(enum_cls: type[JobStatus] | type[JobMode], name: str) -> SAEnum:
    return SAEnum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class Job(Base):
    __tablename__ = "jobs"
    # Fetch server defaults (created_at) via RETURNING on INSERT, so the object is complete after
    # commit without a lazy refresh. SQLAlchemy types this as an instance attribute, hence noqa.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key"),
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_sha", "payload_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    mode: Mapped[JobMode] = mapped_column(_pg_enum(JobMode, "job_mode"))
    status: Mapped[JobStatus] = mapped_column(_pg_enum(JobStatus, "job_status"))
    schema_id: Mapped[str] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    payload_sha256: Mapped[str] = mapped_column(CHAR(64))
    s3_key: Mapped[str] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class JobEvent(Base):
    __tablename__ = "job_events"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (Index("ix_job_events_job", "job_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)  # SSE event id
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ExtractionResult(Base):
    __tablename__ = "extraction_results"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    schema_id: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    document_validity_score: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    model_reported_validity: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    warnings: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class LlmCallAudit(Base):
    """One row per Gemini attempt. No FK to ``jobs`` on purpose: the audit row must be writable
    even when the job row failed to persist (graceful degradation, ADR-05)."""

    __tablename__ = "llm_call_audit"
    __table_args__ = (
        Index("ix_audit_job", "job_id"),
        Index("ix_audit_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    attempt: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    input_mode: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(Text)
    finish_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
