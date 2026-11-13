"""Builds the production extraction pipeline from settings (used by the API and the worker)."""

from google import genai

from docintel.extraction.gemini_client import GeminiClient
from docintel.extraction.pipeline import ExtractionPipeline
from docintel.extraction.resilience import RetryPolicy
from docintel.settings import Settings


def build_pipeline(
    settings: Settings, client: genai.Client, *, max_attempts: int | None = None
) -> ExtractionPipeline:
    """``max_attempts`` is the per-model retry budget: the sync API's default is small (a client is
    waiting); the async worker passes a larger one (it can afford patience)."""
    attempts = max_attempts or settings.retry_max_attempts_sync
    gemini = GeminiClient(
        client,
        model=settings.gemini_model,
        timeout_s=settings.gemini_timeout_s,
        inline_max_bytes=settings.gemini_inline_max_bytes,
    )
    fallback = settings.gemini_fallback_model or None  # "" disables failover
    if fallback == settings.gemini_model:
        fallback = None
    # With a fallback available, give the primary fewer tries: during a provider brown-out, failing
    # over quickly beats burning the deadline (and free-tier quota) on an overloaded model.
    primary_attempts = settings.gemini_fallback_after_attempts if fallback else attempts
    return ExtractionPipeline(
        gemini,
        retry_policy=RetryPolicy(max_attempts=primary_attempts),
        fallback_model=fallback,
        fallback_policy=RetryPolicy(max_attempts=attempts),
        low_confidence_threshold=settings.confidence_threshold,
    )
