"""Deadline-aware retry with exponential backoff and full jitter (NFR-03).

Hand-rolled (~40 lines) rather than tenacity because the policy needs three things together:
honour ``Retry-After``, never start an attempt that cannot finish inside the caller's deadline,
and report every attempt (success or failure) for the audit log.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from docintel.extraction.gemini_client import GeminiCallError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    min_attempt_budget_s: float = 2.0  # don't start an attempt with less time left than this


class DeadlineExceededError(Exception):
    pass


def backoff_delay(policy: RetryPolicy, attempt: int, retry_after_s: float | None) -> float:
    """Full jitter (AWS architecture blog): uniform(0, min(cap, base * 2^attempt)). Spreads
    retries from many clients so a provider brown-out isn't followed by a synchronized stampede."""
    jittered = random.uniform(0, min(policy.max_delay_s, policy.base_delay_s * 2**attempt))  # noqa: S311
    return max(jittered, retry_after_s or 0.0)


async def with_retries[T](
    call: Callable[[int], Awaitable[T]],
    *,
    policy: RetryPolicy,
    deadline: float,
    on_failure: Callable[[int, GeminiCallError], None],
) -> T:
    """``call(attempt)`` is retried on retryable ``GeminiCallError``s. ``deadline`` is a
    ``time.monotonic()`` timestamp."""
    attempt = 1
    while True:
        if deadline - time.monotonic() < policy.min_attempt_budget_s:
            raise DeadlineExceededError("not enough time left for another attempt")
        try:
            return await call(attempt)
        except GeminiCallError as exc:
            on_failure(attempt, exc)
            if not exc.retryable or attempt >= policy.max_attempts:
                raise
            if exc.retry_after_s is not None and (
                time.monotonic() + exc.retry_after_s + policy.min_attempt_budget_s > deadline
            ):
                # The provider told us when it will accept this model again (e.g. a daily quota
                # resetting in hours) and that's past our deadline: retrying here is pure waste.
                # Re-raise the retryable error so the caller fails over or returns 503.
                raise
            delay = backoff_delay(policy, attempt, exc.retry_after_s)
            if time.monotonic() + delay + policy.min_attempt_budget_s > deadline:
                raise DeadlineExceededError("retry would exceed the deadline") from exc
            await asyncio.sleep(delay)
            attempt += 1
