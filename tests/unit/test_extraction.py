import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from docintel.domain.errors import (
    ExtractionFailedError,
    ExtractionTimeoutError,
    OutputTruncatedError,
    SchemaValidationFailedError,
    UpstreamUnavailableError,
)
from docintel.extraction.confidence import score
from docintel.extraction.pipeline import Attempt
from docintel.extraction.pricing import UnknownModelPriceError, cost_usd, price_for
from docintel.extraction.resilience import (
    DeadlineExceededError,
    RetryPolicy,
    backoff_delay,
    with_retries,
)
from docintel.schemas.common import ExtractionWarning, RuleViolation
from docintel.schemas.registry import get_schema
from tests.fakes import (
    FAST_RETRIES,
    ScriptedGemini,
    call_error,
    cv,
    invoice_payload,
    pipeline_with,
    response,
)

INVOICE = get_schema("invoice.v1")


async def _run(gemini: ScriptedGemini, *, deadline_s: float = 30) -> tuple[object, list[Attempt]]:
    attempts: list[Attempt] = []
    outcome = await pipeline_with(gemini).run(
        document=b"%PDF-1.7 fake",
        mime_type="application/pdf",
        spec=INVOICE,
        deadline=time.monotonic() + deadline_s,
        attempts=attempts,
    )
    return outcome, attempts


# ----------------------------------------------------------------------------- scoring
def test_score_combines_model_rules_and_critical_confidence() -> None:
    violation = RuleViolation(ExtractionWarning(code="X", detail="x"), penalty=0.8)
    result = score(
        model_validity=0.9,
        violations=[violation],
        critical_confidences=[0.9, 0.45],
        field_confidences={"a": 0.95, "b": 0.45},
        low_confidence_threshold=0.7,
    )
    # 0.9 * 0.8 * min(1, mean(0.9, 0.45)=0.675 / 0.9 = 0.75) = 0.54
    assert result.adjusted == 0.54
    assert result.model_reported == 0.9
    assert result.low_confidence_fields == ("b",)


def test_score_is_capped_by_model_validity_when_everything_is_confident() -> None:
    result = score(
        model_validity=0.8,
        violations=[],
        critical_confidences=[0.99, 0.99],
        field_confidences={},
        low_confidence_threshold=0.7,
    )
    assert result.adjusted == 0.8  # high field confidence can't exceed the document score


# ----------------------------------------------------------------------------- pricing
def test_price_changes_on_2027_01_01() -> None:
    assert price_for(
        "gemini-3.8-flash", datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    ).input_per_mtok == Decimal("0.75")
    assert price_for(
        "gemini-3.8-flash", datetime(2027, 1, 1, tzinfo=UTC)
    ).input_per_mtok == Decimal("1.50")


def test_thinking_tokens_are_billed_at_the_output_rate() -> None:
    at = datetime(2026, 11, 11, tzinfo=UTC)
    # 2000 * 0.75/1M + (800 + 200) * 3.75/1M = 0.0015 + 0.00375
    assert cost_usd(
        "gemini-3.8-flash", input_tokens=2000, output_tokens=800, thought_tokens=200, at=at
    ) == Decimal("0.005250")


def test_unknown_model_price() -> None:
    with pytest.raises(UnknownModelPriceError):
        price_for("gemini-9-ultra")


# ----------------------------------------------------------------------------- retries
async def test_retries_retryable_then_succeeds() -> None:
    calls: list[int] = []
    failures: list[int] = []

    async def call(attempt: int) -> str:
        calls.append(attempt)
        if attempt < 3:
            raise call_error(503)
        return "ok"

    result = await with_retries(
        call,
        policy=FAST_RETRIES,
        deadline=time.monotonic() + 10,
        on_failure=lambda n, _: failures.append(n),
    )
    assert (result, calls, failures) == ("ok", [1, 2, 3], [1, 2])


async def test_non_retryable_is_not_retried() -> None:
    calls: list[int] = []

    async def call(attempt: int) -> str:
        calls.append(attempt)
        raise call_error(400, retryable=False)

    with pytest.raises(Exception, match="HTTP 400"):
        await with_retries(
            call, policy=FAST_RETRIES, deadline=time.monotonic() + 10, on_failure=lambda *_: None
        )
    assert calls == [1]


async def test_never_starts_an_attempt_past_the_deadline() -> None:
    async def call(_: int) -> str:
        raise AssertionError("must not be called")

    with pytest.raises(DeadlineExceededError):
        await with_retries(
            call,
            policy=RetryPolicy(min_attempt_budget_s=5),
            deadline=time.monotonic() + 1,
            on_failure=lambda *_: None,
        )


def test_backoff_honours_retry_after_and_cap() -> None:
    policy = RetryPolicy(base_delay_s=0.5, max_delay_s=8)
    assert all(0 <= backoff_delay(policy, 10, None) <= 8 for _ in range(200))
    assert backoff_delay(policy, 1, retry_after_s=7) >= 7


# ----------------------------------------------------------------------------- pipeline
async def test_successful_extraction_scores_and_audits() -> None:
    gemini = ScriptedGemini(response(invoice_payload()))
    outcome, attempts = await _run(gemini)
    assert outcome.data["total_amount"]["value"] == "1180.00"  # Decimal serialised as string
    assert outcome.data["invoice_date"]["value"] == "2026-10-01"
    assert outcome.warnings == []
    assert 0.9 < outcome.validity.adjusted <= 0.96
    assert outcome.validity.low_confidence_fields == ("vendor_tax_id",)
    [attempt] = attempts
    assert (attempt.outcome, attempt.input_tokens, attempt.thought_tokens) == ("SUCCESS", 2000, 200)
    assert attempt.cost_usd is not None
    assert attempt.cost_usd > 0
    assert len(attempt.prompt_version) == 12
    call = gemini.calls[0]
    assert call["thinking_level"] == "low"
    assert call["json_schema"] == INVOICE.json_schema


async def test_rule_violation_lowers_score_and_surfaces_warning() -> None:
    outcome, _ = await _run(ScriptedGemini(response(invoice_payload(total_amount=cv(1500.0)))))
    assert [w.code for w in outcome.warnings] == ["TOTAL_MISMATCH"]
    assert outcome.validity.adjusted < 0.85


async def test_invalid_json_gets_one_repair_attempt() -> None:
    gemini = ScriptedGemini(response('{"invoice_number": "oops"}'), response(invoice_payload()))
    outcome, attempts = await _run(gemini)
    assert [a.outcome for a in attempts] == ["INVALID_OUTPUT", "SUCCESS"]
    assert "did not match the response schema" in gemini.calls[1]["prompt"]
    assert "invoice_number" in gemini.calls[1]["prompt"]  # quotes the actual validation errors
    assert outcome.cost_usd == sum(a.cost_usd for a in attempts)  # both calls are billed


async def test_invalid_twice_fails() -> None:
    with pytest.raises(SchemaValidationFailedError):
        await _run(ScriptedGemini(response("not json"), response("{}")))


async def test_truncated_output_retries_with_bigger_budget() -> None:
    gemini = ScriptedGemini(response("{", status="incomplete"), response(invoice_payload()))
    _, attempts = await _run(gemini)
    assert [a.outcome for a in attempts] == ["INCOMPLETE", "SUCCESS"]
    assert gemini.calls[1]["max_output_tokens"] == 2 * gemini.calls[0]["max_output_tokens"]


async def test_truncated_twice_fails() -> None:
    with pytest.raises(OutputTruncatedError):
        await _run(
            ScriptedGemini(response("{", status="incomplete"), response("{", status="incomplete"))
        )


async def test_failed_status_is_not_retried() -> None:
    gemini = ScriptedGemini(response("", status="failed"))
    with pytest.raises(ExtractionFailedError):
        await _run(gemini)
    assert len(gemini.calls) == 1


async def test_provider_outage_exhausts_retries_and_records_every_attempt() -> None:
    attempts: list[Attempt] = []
    with pytest.raises(UpstreamUnavailableError):
        await pipeline_with(ScriptedGemini(*(call_error(503) for _ in range(3)))).run(
            document=b"%PDF-",
            mime_type="application/pdf",
            spec=INVOICE,
            deadline=time.monotonic() + 30,
            attempts=attempts,
        )
    assert [(a.outcome, a.http_status) for a in attempts] == [("RETRYABLE_ERROR", 503)] * 3


async def test_bad_request_is_extraction_failed() -> None:
    with pytest.raises(ExtractionFailedError, match="rejected the request"):
        await _run(ScriptedGemini(call_error(400, retryable=False)))


async def test_no_time_left_is_a_timeout() -> None:
    with pytest.raises(ExtractionTimeoutError):
        await _run(ScriptedGemini(response(invoice_payload())), deadline_s=0.001)


async def test_sync_capacity_rejects_instead_of_queueing() -> None:
    from typing import Any, cast

    from docintel.domain.errors import SyncCapacityExhaustedError
    from docintel.ingestion.service import IngestionService
    from tests.conftest import make_settings

    service = IngestionService(
        settings=make_settings(sync_max_concurrency=1),
        session_factory=cast(Any, None),
        store=cast(Any, None),
        queue=cast(Any, None),
        pipeline=pipeline_with(ScriptedGemini()),
    )
    async with service._sync_slots:  # the only slot is busy
        with pytest.raises(SyncCapacityExhaustedError):
            await service.extract_sync(cast(Any, None), INVOICE, request_id="r", client_id=None)
