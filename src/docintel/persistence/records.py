"""Mapping from extraction results to database rows (shared by the sync API path and the worker)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from docintel.extraction.pipeline import Attempt, ExtractionOutcome
from docintel.persistence.models import ExtractionResult, Job, LlmCallAudit


def result_row(job: Job, outcome: ExtractionOutcome) -> ExtractionResult:
    return ExtractionResult(
        job_id=job.id,
        schema_id=job.schema_id,
        result=outcome.data,
        document_validity_score=Decimal(str(outcome.validity.adjusted)),
        model_reported_validity=Decimal(str(outcome.validity.model_reported)),
        warnings=[w.model_dump() for w in outcome.warnings],
    )


def audit_row(job_id: uuid.UUID, a: Attempt) -> LlmCallAudit:
    return LlmCallAudit(
        job_id=job_id,
        attempt=a.attempt,
        model=a.model,
        prompt_version=a.prompt_version,
        input_mode=a.input_mode,
        prompt_tokens=a.input_tokens,
        output_tokens=a.output_tokens,
        thinking_tokens=a.thought_tokens,
        total_tokens=a.total_tokens,
        estimated_cost_usd=a.cost_usd,
        latency_ms=a.latency_ms,
        outcome=a.outcome,
        http_status=a.http_status,
        error_type=a.error_type,
        finish_reason=a.finish_status,
        created_at=a.at,
    )


def jsonable(record: dict[str, Any]) -> dict[str, Any]:
    return {k: str(v) if isinstance(v, Decimal | datetime) else v for k, v in record.items()}
