"""SQS consumer loop with bounded concurrency and graceful shutdown.

Delivery is at-least-once: a message is deleted only after its handler returns. Anything not
deleted (handler error, shutdown timeout, crash) becomes visible again after the queue's
visibility timeout, so handlers must be idempotent (implementation.md §4.10.5).
"""

import asyncio
import contextlib
import random
from collections.abc import Mapping
from typing import Any

import structlog

from docintel.worker.handler import MessageHandler

log = structlog.get_logger(__name__)

_SQS_MAX_BATCH = 10
_RECEIVE_ERROR_BACKOFF_S = (1.0, 5.0)


class Worker:
    def __init__(
        self,
        *,
        sqs: Any,
        queue_url: str,
        handler: MessageHandler,
        concurrency: int,
        wait_time_s: int = 20,
        drain_timeout_s: float = 25.0,
    ) -> None:
        self._sqs = sqs
        self._queue_url = queue_url
        self._handler = handler
        self._concurrency = concurrency
        self._wait_time_s = wait_time_s
        self._drain_timeout_s = drain_timeout_s
        self._stop = asyncio.Event()
        self._in_flight: set[asyncio.Task[None]] = set()

    def request_stop(self) -> None:
        if not self._stop.is_set():
            log.info("worker_stop_requested", in_flight=len(self._in_flight))
            self._stop.set()

    async def run(self) -> None:
        log.info("worker_started", concurrency=self._concurrency)
        while not self._stop.is_set():
            free_slots = self._concurrency - len(self._in_flight)
            if free_slots <= 0:
                await self._wait_for_slot_or_stop()
                continue
            for message in await self._receive(min(free_slots, _SQS_MAX_BATCH)):
                task = asyncio.create_task(self._process(message))
                self._in_flight.add(task)
                task.add_done_callback(self._in_flight.discard)
        await self._drain()
        log.info("worker_stopped")

    async def _receive(self, max_messages: int) -> list[Mapping[str, Any]]:
        receive = asyncio.ensure_future(
            self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=self._wait_time_s,
                MessageSystemAttributeNames=["ApproximateReceiveCount"],
                AttributeNames=["ApproximateReceiveCount"],  # legacy name; emulators need it
                MessageAttributeNames=["All"],
            )
        )
        stopped = asyncio.ensure_future(self._stop.wait())
        await asyncio.wait({receive, stopped}, return_when=asyncio.FIRST_COMPLETED)
        stopped.cancel()
        if not receive.done():
            # Shutdown mid long-poll. Any message SQS hands out in this instant is simply
            # never deleted and reappears after the visibility timeout.
            receive.cancel()
            return []
        try:
            response = receive.result()
        except Exception:
            log.exception("sqs_receive_failed")
            await self._sleep_unless_stopped(random.uniform(*_RECEIVE_ERROR_BACKOFF_S))  # noqa: S311
            return []
        messages: list[Mapping[str, Any]] = response.get("Messages", [])
        return messages

    async def _process(self, message: Mapping[str, Any]) -> None:
        attributes = message.get("MessageAttributes", {})
        with structlog.contextvars.bound_contextvars(
            message_id=message.get("MessageId"),
            receive_count=message.get("Attributes", {}).get("ApproximateReceiveCount"),
            request_id=attributes.get("request_id", {}).get("StringValue"),
        ):
            try:
                await self._handler.handle(message)
                await self._sqs.delete_message(
                    QueueUrl=self._queue_url, ReceiptHandle=message["ReceiptHandle"]
                )
            except asyncio.CancelledError:
                log.warning("message_processing_cancelled")
                raise
            except Exception:
                log.exception("message_processing_failed")

    async def _wait_for_slot_or_stop(self) -> None:
        stopped = asyncio.ensure_future(self._stop.wait())
        await asyncio.wait({*self._in_flight, stopped}, return_when=asyncio.FIRST_COMPLETED)
        stopped.cancel()

    async def _drain(self) -> None:
        if not self._in_flight:
            return
        log.info("worker_draining", in_flight=len(self._in_flight))
        _, pending = await asyncio.wait(set(self._in_flight), timeout=self._drain_timeout_s)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            log.warning("worker_drain_timeout", abandoned=len(pending))

    async def _sleep_unless_stopped(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
