"""Extraction pipeline: one document + one schema -> validated, scored domain result.

Shared by the sync API path (Phase 3) and the async worker (Phase 4), so it knows nothing about
HTTP, SQS or the database. Every model call is recorded as an ``Attempt`` in a caller-owned list,
including failed ones, so the caller can persist the audit trail whether or not the run succeeds.

Recovery policy per attempt outcome (implementation.md §4.7.4):
* transport / 429 / 5xx      -> retried with backoff inside the deadline (resilience.py)
* status ``incomplete``      -> one retry with double the output-token budget, then OUTPUT_TRUNCATED
* JSON fails the wire schema -> one "repair" retry quoting the validation errors, then
                                SCHEMA_VALIDATION_FAILED
* any other status / 4xx     -> EXTRACTION_FAILED (not retryable)
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from docintel.domain.errors import (
    ExtractionFailedError,
    ExtractionTimeoutError,
    OutputTruncatedError,
    SchemaValidationFailedError,
    UpstreamUnavailableError,
)
from docintel.extraction.confidence import ValidityScore, score
from docintel.extraction.gemini_client import GeminiCallError, GeminiClient, GeminiResponse
from docintel.extraction.model_profiles import thinking_level_for
from docintel.extraction.pricing import UnknownModelPriceError, cost_usd
from docintel.extraction.prompts import SYSTEM_INSTRUCTION, prompt_version, repair_prompt
from docintel.extraction.resilience import DeadlineExceededError, RetryPolicy, with_retries
from docintel.schemas.common import Extracted, ExtractionWarning
from docintel.schemas.registry import SchemaSpec

log = structlog.get_logger(__name__)

MAX_OUTPUT_TOKENS_CAP = 65_536  # gemini-3.8-flash output limit


@dataclass
class Attempt:
    """One model call; becomes one ``llm_call_audit`` row."""

    attempt: int
    model: str
    prompt_version: str
    input_mode: str
    latency_ms: int
    outcome: str  # SUCCESS | RETRYABLE_ERROR | FATAL_ERROR | INVALID_OUTPUT | INCOMPLETE
    at: datetime
    input_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: Decimal | None = None
    http_status: int | None = None
    error_type: str | None = None
    finish_status: str | None = None


@dataclass
class ExtractionOutcome:
    schema_id: str
    data: dict[str, Any]
    validity: ValidityScore
    warnings: list[ExtractionWarning]
    gemini_ms: int
    validation_ms: int
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(a.input_tokens or 0 for a in self.attempts)

    @property
    def output_tokens(self) -> int:
        return sum(a.output_tokens or 0 for a in self.attempts)

    @property
    def thought_tokens(self) -> int:
        return sum(a.thought_tokens or 0 for a in self.attempts)

    @property
    def cost_usd(self) -> Decimal:
        return sum((a.cost_usd or Decimal(0) for a in self.attempts), Decimal(0))


def _validation_summary(exc: ValidationError, limit: int = 10) -> str:
    lines = [
        f"- {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()[:limit]
    ]
    return "\n".join(lines)


def _field_confidences(domain: BaseModel) -> dict[str, float]:
    return {
        name: value.confidence
        for name in type(domain).model_fields
        if isinstance(value := getattr(domain, name), Extracted)
    }


class ExtractionPipeline:
    def __init__(
        self,
        gemini: GeminiClient,
        *,
        retry_policy: RetryPolicy,
        low_confidence_threshold: float,
        fallback_model: str | None = None,
        fallback_policy: RetryPolicy | None = None,
    ) -> None:
        self._gemini = gemini
        self._retry_policy = retry_policy  # primary model
        self._fallback_policy = fallback_policy or retry_policy
        self._low_confidence_threshold = low_confidence_threshold
        self._models = [gemini.model]
        if fallback_model and fallback_model != gemini.model:
            self._models.append(fallback_model)

    async def run(
        self,
        *,
        document: bytes,
        mime_type: str,
        spec: SchemaSpec,
        deadline: float,
        attempts: list[Attempt],
    ) -> ExtractionOutcome:
        version = prompt_version(SYSTEM_INSTRUCTION, spec.prompt, spec.json_schema)
        prompt = spec.prompt
        max_tokens = spec.max_output_tokens
        repaired = extended = False
        active = 0  # index into self._models; sticky once we've failed over

        while True:
            response, active = await self._call(
                document, mime_type, spec, prompt, max_tokens, version, deadline, attempts, active
            )
            if response.status == "incomplete":
                self._record(attempts, response, version, "INCOMPLETE")
                if not extended and max_tokens < MAX_OUTPUT_TOKENS_CAP:
                    max_tokens, extended = min(max_tokens * 2, MAX_OUTPUT_TOKENS_CAP), True
                    continue
                raise OutputTruncatedError("The model ran out of output tokens.")
            if response.status != "completed":
                self._record(attempts, response, version, "FATAL_ERROR")
                detail = "; ".join(response.error_messages) or f"status {response.status!r}"
                raise ExtractionFailedError(f"The model did not complete: {detail[:300]}")
            try:
                wire = spec.wire_model.model_validate_json(response.text)
            except ValidationError as exc:
                self._record(attempts, response, version, "INVALID_OUTPUT")
                if not repaired:
                    prompt, repaired = repair_prompt(spec.prompt, _validation_summary(exc)), True
                    continue
                raise SchemaValidationFailedError(
                    "The model's output did not match the schema after a repair attempt."
                ) from exc
            self._record(attempts, response, version, "SUCCESS")
            break

        validation_start = time.perf_counter()
        domain, warnings = spec.to_domain(wire)
        violations = spec.rules(domain)
        confidences = _field_confidences(domain)
        validity = score(
            model_validity=float(getattr(wire, "document_validity_score", 0.0)),
            violations=violations,
            critical_confidences=[confidences[f] for f in spec.critical_fields],
            field_confidences=confidences,
            low_confidence_threshold=self._low_confidence_threshold,
        )
        return ExtractionOutcome(
            schema_id=spec.schema_id,
            data=domain.model_dump(mode="json"),
            validity=validity,
            warnings=[*warnings, *(v.warning for v in violations)],
            gemini_ms=sum(a.latency_ms for a in attempts),
            validation_ms=int((time.perf_counter() - validation_start) * 1000),
            attempts=attempts,
        )

    async def _call(
        self,
        document: bytes,
        mime_type: str,
        spec: SchemaSpec,
        prompt: str,
        max_tokens: int,
        version: str,
        deadline: float,
        attempts: list[Attempt],
        start: int,
    ) -> tuple[GeminiResponse, int]:
        """Try models in order from ``start``. A retryable failure that exhausts one model's
        policy fails over to the next; non-retryable errors (4xx: our request is wrong) never do.
        Returns the response and the index of the model that served it."""
        for index in range(start, len(self._models)):
            model = self._models[index]
            has_next = index + 1 < len(self._models)
            policy = self._retry_policy if index == 0 else self._fallback_policy
            try:
                response = await with_retries(
                    self._attempt_fn(
                        document, mime_type, spec, prompt, max_tokens, deadline, model
                    ),
                    policy=policy,
                    deadline=deadline,
                    on_failure=self._failure_recorder(attempts, version, model),
                )
                return response, index
            except DeadlineExceededError as exc:
                if has_next and deadline - time.monotonic() >= policy.min_attempt_budget_s:
                    self._log_failover(model, self._models[index + 1], "deadline")
                    continue
                raise ExtractionTimeoutError(
                    "Extraction did not finish within the deadline; retry or use mode=async."
                ) from exc
            except GeminiCallError as exc:
                if exc.retryable and has_next:
                    self._log_failover(model, self._models[index + 1], exc.error_type)
                    continue
                if exc.retryable:
                    raise UpstreamUnavailableError(
                        "The AI provider is temporarily unavailable; please retry."
                    ) from exc
                raise ExtractionFailedError(
                    f"The AI provider rejected the request ({exc.error_type})."
                ) from exc
        raise RuntimeError("no model configured")  # unreachable: _models is never empty

    @staticmethod
    def _log_failover(from_model: str, to_model: str, reason: str) -> None:
        log.warning("gemini_failover", from_model=from_model, to_model=to_model, reason=reason)

    def _failure_recorder(
        self, attempts: list[Attempt], version: str, model: str
    ) -> Callable[[int, GeminiCallError], None]:
        def on_failure(_: int, exc: GeminiCallError) -> None:
            attempts.append(
                Attempt(
                    attempt=len(attempts) + 1,
                    model=model,
                    prompt_version=version,
                    input_mode="UNKNOWN",
                    latency_ms=exc.latency_ms,
                    outcome="RETRYABLE_ERROR" if exc.retryable else "FATAL_ERROR",
                    at=datetime.now(UTC),
                    http_status=exc.http_status,
                    error_type=exc.error_type,
                )
            )
            log.warning(
                "gemini_call_failed",
                model=model,
                attempt=len(attempts),
                retryable=exc.retryable,
                http_status=exc.http_status,
                error_type=exc.error_type,
                latency_ms=exc.latency_ms,
                provider_message=exc.provider_message,
            )

        return on_failure

    def _attempt_fn(
        self,
        document: bytes,
        mime_type: str,
        spec: SchemaSpec,
        prompt: str,
        max_tokens: int,
        deadline: float,
        model: str,
    ) -> Callable[[int], Awaitable[GeminiResponse]]:
        async def attempt(_: int) -> GeminiResponse:
            remaining = max(1.0, deadline - time.monotonic())
            return await self._gemini.generate(
                document=document,
                mime_type=mime_type,
                system_instruction=SYSTEM_INSTRUCTION,
                prompt=prompt,
                json_schema=spec.json_schema,
                thinking_level=thinking_level_for(model),
                max_output_tokens=max_tokens,
                timeout_s=remaining,
                model=model,
            )

        return attempt

    def _record(
        self, attempts: list[Attempt], response: GeminiResponse, version: str, outcome: str
    ) -> None:
        usage = response.usage
        model = response.model or self._gemini.model
        try:
            cost: Decimal | None = cost_usd(
                model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                thought_tokens=usage.thought_tokens,
            )
        except UnknownModelPriceError:
            log.error("unknown_model_price", model=model)
            cost = None  # never block an extraction on a missing price; alarm instead
        attempts.append(
            Attempt(
                attempt=len(attempts) + 1,
                model=model,
                prompt_version=version,
                input_mode=response.input_mode,
                latency_ms=response.latency_ms,
                outcome=outcome,
                at=datetime.now(UTC),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                thought_tokens=usage.thought_tokens,
                total_tokens=usage.total_tokens,
                cost_usd=cost,
                finish_status=response.status,
            )
        )
