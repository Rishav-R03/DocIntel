"""Initial schema: jobs, job_events, extraction_results, llm_call_audit (implementation.md §4.8).

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATUS = postgresql.ENUM(
    "RECEIVED",
    "STAGED",
    "PROCESSING",
    "VALIDATING",
    "COMPLETED",
    "FAILED",
    name="job_status",
    create_type=False,
)
JOB_MODE = postgresql.ENUM("SYNC", "ASYNC", name="job_mode", create_type=False)

NOW = sa.text("now()")


def upgrade() -> None:
    JOB_STATUS.create(op.get_bind(), checkfirst=False)
    JOB_MODE.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mode", JOB_MODE, nullable=False),
        sa.Column("status", JOB_STATUS, nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("payload_sha256", sa.CHAR(64), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.UniqueConstraint(
            "client_id", "idempotency_key", name="uq_jobs_client_id_idempotency_key"
        ),
    )
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])
    op.create_index("ix_jobs_sha", "jobs", ["payload_sha256"])

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_job_events"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_job_events_job_id_jobs", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_job_events_job", "job_events", ["job_id", "id"])

    op.create_table(
        "extraction_results",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("document_validity_score", sa.Numeric(4, 3), nullable=False),
        sa.Column("model_reported_validity", sa.Numeric(4, 3), nullable=True),
        sa.Column(
            "warnings", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("job_id", name="pk_extraction_results"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_extraction_results_job_id_jobs", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "llm_call_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("input_mode", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("thinking_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_llm_call_audit"),
    )
    op.create_index("ix_audit_job", "llm_call_audit", ["job_id"])
    op.create_index("ix_audit_created", "llm_call_audit", ["created_at"])


def downgrade() -> None:
    op.drop_table("llm_call_audit")
    op.drop_table("extraction_results")
    op.drop_table("job_events")
    op.drop_table("jobs")
    JOB_MODE.drop(op.get_bind(), checkfirst=False)
    JOB_STATUS.drop(op.get_bind(), checkfirst=False)
