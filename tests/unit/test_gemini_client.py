"""Pins the SDK contract: request shape (privacy, schema, thinking), Files API path, and the
classification of the SDK's (private) error types. An SDK upgrade that changes any of these must
fail here, not in production."""

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai._gaos.errors import genaierror
from google.genai._gaos.lib import compat_errors

from docintel.extraction.gemini_client import GeminiCallError, GeminiClient, classify_error

httpx = genaierror.httpx  # the httpx module the SDK itself was built against


class FakeAio:
    def __init__(self, interaction: Any = None, error: Exception | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.uploaded: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self._interaction = interaction
        self._error = error
        self.interactions = SimpleNamespace(create=self._create)
        self.files = SimpleNamespace(upload=self._upload, delete=self._delete)

    async def _create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        if self._error:
            raise self._error
        return self._interaction

    async def _upload(self, **kwargs: Any) -> Any:
        self.uploaded.append(kwargs)
        return SimpleNamespace(name="files/abc", uri="https://files/abc")

    async def _delete(self, *, name: str) -> None:
        self.deleted.append(name)


def _interaction(text: str = '{"a": 1}', status: str = "completed") -> Any:
    usage = SimpleNamespace(
        total_input_tokens=1500,
        total_output_tokens=300,
        total_thought_tokens=120,
        total_cached_tokens=0,
        total_tokens=1920,
    )
    return SimpleNamespace(output_text=text, status=status, usage=usage, errors=None)


def _client(aio: FakeAio, inline_max: int = 1024) -> GeminiClient:
    return GeminiClient(
        SimpleNamespace(aio=aio),
        model="gemini-3.8-flash",  # type: ignore[arg-type]
        timeout_s=25,
        inline_max_bytes=inline_max,
    )


async def _generate(client: GeminiClient, document: bytes = b"%PDF-1.7 doc", **kw: Any):  # type: ignore[no-untyped-def]
    return await client.generate(
        document=document,
        mime_type="application/pdf",
        system_instruction="SYS",
        prompt="PROMPT",
        json_schema={"type": "object"},
        thinking_level="low",
        max_output_tokens=4096,
        **kw,
    )


async def test_request_shape_privacy_schema_and_no_sampling_params() -> None:
    aio = FakeAio(_interaction())
    result = await _generate(_client(aio), timeout_s=10)
    [body] = aio.created
    assert body["store"] is False  # never persist customer documents on the provider side
    assert body["model"] == "gemini-3.8-flash"
    assert body["response_format"] == {
        "type": "text",
        "mime_type": "application/json",
        "schema": {"type": "object"},
    }
    assert body["generation_config"] == {"thinking_level": "low", "max_output_tokens": 4096}
    assert not {"temperature", "top_p", "top_k", "candidate_count"} & body.keys()
    assert body["system_instruction"] == "SYS"
    doc, text = body["input"]
    assert doc == {
        "type": "document",
        "mime_type": "application/pdf",
        "data": base64.b64encode(b"%PDF-1.7 doc").decode(),
    }
    assert text == {"type": "text", "text": "PROMPT"}
    assert body["timeout"] == 10  # caller's remaining deadline wins over the 25 s default
    assert (result.input_mode, result.usage.thought_tokens, result.status) == (
        "INLINE",
        120,
        "completed",
    )


async def test_large_document_uses_files_api_and_always_deletes() -> None:
    aio = FakeAio(_interaction())
    await _generate(_client(aio, inline_max=4), document=b"%PDF-big")
    assert len(aio.uploaded) == 1
    assert aio.created[0]["input"][0] == {
        "type": "document",
        "uri": "https://files/abc",
        "mime_type": "application/pdf",
    }
    assert aio.deleted == ["files/abc"]


async def test_uploaded_file_deleted_even_when_the_call_fails() -> None:
    aio = FakeAio(error=TimeoutError())
    with pytest.raises(GeminiCallError):
        await _generate(_client(aio, inline_max=4), document=b"%PDF-big")
    assert aio.deleted == ["files/abc"]


def _http_error(status: int, headers: dict[str, str] | None = None) -> Exception:
    raw = httpx.Response(
        status,
        headers=headers or {},
        text="{}",
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com"),
    )
    return genaierror.GenAiError("boom", raw_response=raw)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (429, True),
        (500, True),
        (503, True),
        (504, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
    ],
)
def test_http_errors_are_classified(status: int, retryable: bool) -> None:
    err = classify_error(_http_error(status), latency_ms=5)
    assert (err.http_status, err.retryable) == (status, retryable)


def test_retry_after_header_is_read() -> None:
    assert classify_error(_http_error(429, {"Retry-After": "7"}), 5).retry_after_s == 7.0


def test_connection_errors_are_retryable() -> None:
    conn = compat_errors.APIConnectionError(request=httpx.Request("POST", "https://x"))
    assert classify_error(conn, 5).retryable is True
    assert classify_error(TimeoutError(), 5).retryable is True


def test_unknown_errors_are_fatal() -> None:
    assert classify_error(RuntimeError("weird"), 5).retryable is False


def test_sdk_level_retries_are_disabled() -> None:
    """Regression: the SDK's default (5 attempts / 30 s) stacked under our retry policy, hid
    attempts from the audit log and consumed the sync deadline inside a single attempt."""
    from google.genai._gaos.google_genai import _translate_retry_config

    from docintel.extraction.gemini_client import create_genai_client

    client = create_genai_client("test-key")
    http_options = client._api_client._http_options
    assert http_options.retry_options.attempts == 1
    assert (
        _translate_retry_config(http_options).max_retries == 0
    )  # what the Interactions client uses


def test_provider_message_is_captured_for_diagnosis() -> None:
    err = classify_error(_http_error(429), latency_ms=5)
    assert err.provider_message == "boom"  # e.g. which quota was exhausted, for the logs


async def test_per_call_model_override_is_sent_and_recorded() -> None:
    aio = FakeAio(_interaction())
    result = await _generate(_client(aio), model="gemini-3.5-flash-lite")
    assert aio.created[0]["model"] == "gemini-3.5-flash-lite"
    assert result.model == "gemini-3.5-flash-lite"
