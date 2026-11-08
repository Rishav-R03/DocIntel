"""Message handler contract. The real extraction handler replaces ``LoggingHandler`` in Phase 4."""

from collections.abc import Mapping
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)


class MessageHandler(Protocol):
    async def handle(self, message: Mapping[str, Any]) -> None:
        """Process one SQS message. Return normally to ack; raise to leave it for redelivery."""


class LoggingHandler:
    """Phase 1 placeholder: proves the receive -> handle -> delete loop end to end."""

    async def handle(self, message: Mapping[str, Any]) -> None:
        log.info("message_received", body_bytes=len(message.get("Body", "")))
