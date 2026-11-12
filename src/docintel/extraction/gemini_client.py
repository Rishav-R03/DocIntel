"""Thin adapter over the google-genai Interactions API.

This is the only module that touches the SDK. Everything above it deals in ``GeminiResponse`` and
``GeminiCallError``, so an SDK change (the Interactions client and its error classes currently
live in the SDK's private ``_gaos`` package) is contained here and pinned by
tests/unit/test_gemini_client.py.

Privacy: every request sets ``store=False``. The Interactions API can persist requests and
responses server-side for later retrieval; for customer financial documents that must be opt-in,
never a default we inherit.

Retries: the SDK retries internally by default (5 attempts, up to 30 s). Stacked under our own
deadline-aware policy that multiplied calls to an already-overloaded provider, consumed the whole
latency budget inside one "attempt", and hid attempts from the audit log. ``create_genai_client``
disables SDK retries so exactly one layer (extraction/resilience.py) owns them.
"""

import base64
import io
import time
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from google import genai
from google.genai import types

# Private SDK modules: confined to this file on purpose (see module docstring).
from google.genai._gaos.errors.genaierror import GenAiError
from google.genai._gaos.lib import compat_errors

log = structlog.get_logger(__name__)

InputMode = Literal["INLINE", "FILES_API"]
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def create_genai_client(api_key: str) -> genai.Client:
    """The only way the service builds an SDK client: SDK-level retries off (see docstring)."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )


@dataclass(frozen=True)
class GeminiUsage:
    input_tokens: int
    output_tokens: int
    thought_tokens: int
    cached_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class GeminiResponse:
    text: str
    status: str  # completed | incomplete | failed | ...
    usage: GeminiUsage
    latency_ms: int
    input_mode: InputMode
    error_messages: tuple[str, ...] = ()
    model: str = ""  # the model that actually served this call (primary or fallback)


class GeminiCallError(Exception):
    """A failed call, already classified for the retry policy."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        http_status: int | None,
        error_type: str,
        latency_ms: int,
        retry_after_s: float | None = None,
        provider_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status
        self.error_type = error_type
        self.latency_ms = latency_ms
        self.retry_after_s = retry_after_s
        # Gemini's own explanation (e.g. which quota a 429 hit). Truncated; never contains
        # document content or credentials.
        self.provider_message = provider_message


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("retry-after") if headers is not None else None
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None  # HTTP-date form; fall back to our own backoff


def _provider_message(exc: BaseException) -> str | None:
    text = " ".join(str(getattr(exc, "message", None) or exc).split())
    return text[:300] or None


def classify_error(exc: BaseException, latency_ms: int) -> GeminiCallError:
    """Map any SDK/transport failure to retryable vs fatal. Unknown errors are fatal: retrying an
    error we don't understand burns quota and latency budget for nothing."""
    status = getattr(exc, "status_code", None)
    if isinstance(exc, compat_errors.APIConnectionError | TimeoutError | ConnectionError):
        return GeminiCallError(
            str(exc) or type(exc).__name__,
            retryable=True,
            http_status=None,
            error_type=type(exc).__name__,
            latency_ms=latency_ms,
            provider_message=_provider_message(exc),
        )
    if isinstance(exc, GenAiError | compat_errors.APIStatusError) and isinstance(status, int):
        return GeminiCallError(
            f"Gemini returned HTTP {status}",
            retryable=status in _RETRYABLE_STATUS,
            http_status=status,
            error_type=type(exc).__name__,
            latency_ms=latency_ms,
            retry_after_s=_retry_after(getattr(exc, "headers", None)),
            provider_message=_provider_message(exc),
        )
    return GeminiCallError(
        str(exc) or type(exc).__name__,
        retryable=False,
        http_status=status,
        error_type=type(exc).__name__,
        latency_ms=latency_ms,
        provider_message=_provider_message(exc),
    )


def _usage(interaction: Any) -> GeminiUsage:
    usage = getattr(interaction, "usage", None)

    def tokens(name: str) -> int:
        return int(getattr(usage, name, None) or 0)

    return GeminiUsage(
        input_tokens=tokens("total_input_tokens"),
        output_tokens=tokens("total_output_tokens"),
        thought_tokens=tokens("total_thought_tokens"),
        cached_tokens=tokens("total_cached_tokens"),
        total_tokens=tokens("total_tokens"),
    )


class GeminiClient:
    def __init__(
        self, client: genai.Client, *, model: str, timeout_s: float, inline_max_bytes: int
    ) -> None:
        self._client = client
        self.model = model
        self._timeout_s = timeout_s
        self._inline_max_bytes = inline_max_bytes

    async def generate(
        self,
        *,
        document: bytes,
        mime_type: str,
        system_instruction: str,
        prompt: str,
        json_schema: dict[str, Any],
        thinking_level: str,
        max_output_tokens: int,
        timeout_s: float | None = None,
        model: str | None = None,
    ) -> GeminiResponse:
        """``timeout_s`` (the caller's remaining deadline) can only shorten the configured
        per-call timeout, never extend it."""
        timeout = min(self._timeout_s, timeout_s) if timeout_s else self._timeout_s
        model = model or self.model
        start = time.perf_counter()
        uploaded_name: str | None = None
        try:
            if len(document) <= self._inline_max_bytes:
                input_mode: InputMode = "INLINE"
                doc_part: dict[str, Any] = {
                    "type": "document" if mime_type == "application/pdf" else "image",
                    "data": base64.b64encode(document).decode("ascii"),
                    "mime_type": mime_type,
                }
            else:
                # base64 inflates by a third; large documents go through the Files API instead.
                input_mode = "FILES_API"
                uploaded = await self._client.aio.files.upload(
                    file=io.BytesIO(document), config={"mime_type": mime_type}
                )
                uploaded_name = uploaded.name
                doc_part = {
                    "type": "document" if mime_type == "application/pdf" else "image",
                    "uri": uploaded.uri,
                    "mime_type": mime_type,
                }
            interaction = await self._client.aio.interactions.create(
                model=model,
                input=[doc_part, {"type": "text", "text": prompt}],
                system_instruction=system_instruction,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": json_schema,
                },
                generation_config={
                    "thinking_level": thinking_level,
                    "max_output_tokens": max_output_tokens,
                },
                store=False,
                timeout=timeout,
            )
        except Exception as exc:
            raise classify_error(exc, _elapsed_ms(start)) from exc
        finally:
            if uploaded_name is not None:
                await self._delete_quietly(uploaded_name)

        errors = tuple(
            str(getattr(e, "message", None) or e)
            for e in (getattr(interaction, "errors", None) or ())
        )
        return GeminiResponse(
            text=getattr(interaction, "output_text", None) or "",
            status=str(getattr(interaction, "status", "") or ""),
            usage=_usage(interaction),
            latency_ms=_elapsed_ms(start),
            input_mode=input_mode,
            error_messages=errors,
            model=model,
        )

    async def _delete_quietly(self, name: str) -> None:
        try:
            await self._client.aio.files.delete(name=name)
        except Exception:
            # Not fatal (Files API objects expire after 48 h), but a customer document is left
            # on the provider side until then, so make it visible.
            log.warning("gemini_file_delete_failed", file_name=name, exc_info=True)


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
