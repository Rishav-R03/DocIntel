"""Message handler contract (the extraction handler lives in ``extraction_handler.py``)."""

from collections.abc import Mapping
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)


class RetryLaterError(Exception):
    """Raised by a handler to have the message redelivered after `delay_s` (not deleted)."""

    def __init__(self, reason: str, *, delay_s: int) -> None:
        super().__init__(reason)
        self.delay_s = delay_s


class MessageHandler(Protocol):
    async def handle(self, message: Mapping[str, Any]) -> None:
        """Process one SQS message. Return normally to ack; raise to leave it for redelivery."""


class LoggingHandler:
    """Phase 1 placeholder: proves the receive -> handle -> delete loop end to end."""

    async def handle(self, message: Mapping[str, Any]) -> None:
        log.info("message_received", body_bytes=len(message.get("Body", "")))
