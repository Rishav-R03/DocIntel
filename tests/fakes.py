"""Scripted Gemini stand-in and payload builders for extraction tests."""

import copy
import json
from typing import Any, cast

from docintel.extraction.gemini_client import (
    GeminiCallError,
    GeminiClient,
    GeminiResponse,
    GeminiUsage,
)
from docintel.extraction.pipeline import ExtractionPipeline
from docintel.extraction.resilience import RetryPolicy

USAGE = GeminiUsage(
    input_tokens=2000, output_tokens=800, thought_tokens=200, cached_tokens=0, total_tokens=3000
)
FAST_RETRIES = RetryPolicy(
    max_attempts=3, base_delay_s=0.001, max_delay_s=0.01, min_attempt_budget_s=0.01
)


def response(
    payload: dict[str, Any] | str, *, status: str = "completed", usage: GeminiUsage = USAGE
) -> GeminiResponse:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return GeminiResponse(
        text=text, status=status, usage=usage, latency_ms=1200, input_mode="INLINE"
    )


def call_error(
    http_status: int | None = 503, *, retryable: bool = True, retry_after_s: float | None = None
) -> GeminiCallError:
    return GeminiCallError(
        f"HTTP {http_status}",
        retryable=retryable,
        http_status=http_status,
        error_type="FakeError",
        latency_ms=50,
        retry_after_s=retry_after_s,
    )


class ScriptedGemini:
    """Replays a script of responses/errors; records every call's keyword arguments."""

    model = "gemini-3.8-flash"

    def __init__(self, *script: GeminiResponse | GeminiCallError) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> GeminiResponse:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def pipeline_with(gemini: ScriptedGemini) -> ExtractionPipeline:
    return ExtractionPipeline(
        cast(GeminiClient, gemini), retry_policy=FAST_RETRIES, low_confidence_threshold=0.7
    )


def cv(value: Any, confidence: float = 0.97, page: int | None = 1) -> dict[str, Any]:
    return {"value": value, "confidence": confidence, "page": page}


_INVOICE: dict[str, Any] = {
    "invoice_number": cv("INV-2041"),
    "invoice_date": cv("2026-10-01"),
    "due_date": cv("2026-10-31"),
    "currency": cv("usd"),
    "vendor_name": cv("Acme   Corp"),
    "vendor_tax_id": cv(None, 0.2, None),
    "customer_name": cv("Globex Ltd"),
    "subtotal": cv(1000.0),
    "tax_amount": cv(180.0),
    "total_amount": cv(1180.0, page=2),
    "line_items": [
        {"description": cv("Widget"), "quantity": cv(2), "unit_price": cv(250), "amount": cv(500)},
        {"description": cv("Gadget"), "quantity": cv(1), "unit_price": cv(500), "amount": cv(500)},
    ],
    "document_validity_score": 0.96,
}


def invoice_payload(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(_INVOICE)
    payload.update(overrides)
    return payload
