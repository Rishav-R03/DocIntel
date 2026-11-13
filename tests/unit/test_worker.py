import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from docintel.worker.runner import Worker

QUEUE = "http://sqs.local/000000000000/jobs"


class FakeSQS:
    """Serves queued batches, then blocks like an empty long poll."""

    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self._batches = batches
        self.deleted: list[str] = []
        self.visibility: list[tuple[str, int]] = []
        self.receive_calls = 0

    async def receive_message(self, *, MaxNumberOfMessages: int, **_: Any) -> dict[str, Any]:
        self.receive_calls += 1
        if self._batches:
            batch = self._batches.pop(0)
            if len(batch) > MaxNumberOfMessages:
                self._batches.insert(0, batch[MaxNumberOfMessages:])
            return {"Messages": batch[:MaxNumberOfMessages]}
        await asyncio.sleep(3600)
        return {}

    async def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted.append(ReceiptHandle)

    async def change_message_visibility(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
        VisibilityTimeout: int,
    ) -> None:
        self.visibility.append((ReceiptHandle, VisibilityTimeout))


class RecordingHandler:
    def __init__(self, fail_on: set[str] | None = None, block: asyncio.Event | None = None) -> None:
        self.handled: list[str] = []
        self._fail_on = fail_on or set()
        self._block = block

    async def handle(self, message: Mapping[str, Any]) -> None:
        if self._block:
            await self._block.wait()
        if message["MessageId"] in self._fail_on:
            raise RuntimeError("boom")
        self.handled.append(message["MessageId"])


def _msg(n: int) -> dict[str, Any]:
    return {"MessageId": f"m{n}", "ReceiptHandle": f"r{n}", "Body": "{}"}


async def _until(predicate: Any, within_s: float = 2.0) -> None:
    async with asyncio.timeout(within_s):
        while not predicate():  # noqa: ASYNC110 - polling a fake is fine in tests
            await asyncio.sleep(0.01)


def _worker(sqs: FakeSQS, handler: Any, **kwargs: Any) -> Worker:
    return Worker(sqs=sqs, queue_url=QUEUE, handler=handler, concurrency=4, **kwargs)


async def test_processes_and_deletes_messages() -> None:
    sqs = FakeSQS([[_msg(1), _msg(2)], [_msg(3)]])
    worker = _worker(sqs, RecordingHandler())
    run = asyncio.create_task(worker.run())

    await _until(lambda: len(sqs.deleted) == 3)
    worker.request_stop()
    await asyncio.wait_for(run, timeout=1)  # also proves stop interrupts the long poll

    assert sorted(sqs.deleted) == ["r1", "r2", "r3"]


async def test_failed_message_is_not_deleted() -> None:
    sqs = FakeSQS([[_msg(1), _msg(2)]])
    handler = RecordingHandler(fail_on={"m1"})
    worker = _worker(sqs, handler)
    run = asyncio.create_task(worker.run())

    await _until(lambda: handler.handled == ["m2"] and sqs.deleted == ["r2"])
    worker.request_stop()
    await asyncio.wait_for(run, timeout=1)

    assert sqs.deleted == ["r2"]  # r1 left for redelivery -> eventually DLQ


async def test_stop_drains_in_flight_messages() -> None:
    release = asyncio.Event()
    sqs = FakeSQS([[_msg(1)]])
    worker = _worker(sqs, RecordingHandler(block=release))
    run = asyncio.create_task(worker.run())

    await _until(lambda: sqs.receive_calls >= 2)  # m1 in flight, back in long poll
    worker.request_stop()
    await asyncio.sleep(0.05)
    assert not run.done()  # waiting for the in-flight message

    release.set()
    await asyncio.wait_for(run, timeout=1)
    assert sqs.deleted == ["r1"]


async def test_drain_timeout_abandons_stuck_messages_without_ack() -> None:
    sqs = FakeSQS([[_msg(1)]])
    worker = _worker(sqs, RecordingHandler(block=asyncio.Event()), drain_timeout_s=0.05)
    run = asyncio.create_task(worker.run())

    await _until(lambda: sqs.receive_calls >= 2)
    worker.request_stop()
    await asyncio.wait_for(run, timeout=1)
    assert sqs.deleted == []


async def test_respects_concurrency_limit() -> None:
    release = asyncio.Event()
    sqs = FakeSQS([[_msg(n) for n in range(10)]])
    worker = Worker(
        sqs=sqs, queue_url=QUEUE, handler=RecordingHandler(block=release), concurrency=2
    )
    run = asyncio.create_task(worker.run())

    await asyncio.sleep(0.05)
    assert sqs.receive_calls == 1  # took 2 of 10, then stopped polling while saturated
    assert len(worker._in_flight) == 2

    release.set()
    worker.request_stop()
    await asyncio.wait_for(run, timeout=1)


@pytest.mark.parametrize("error", [ConnectionError("network"), RuntimeError("throttled")])
async def test_receive_errors_back_off_and_still_stop(error: Exception) -> None:
    class FlakySQS(FakeSQS):
        async def receive_message(self, **_: Any) -> dict[str, Any]:
            self.receive_calls += 1
            raise error

    sqs = FlakySQS([])
    worker = _worker(sqs, RecordingHandler())
    run = asyncio.create_task(worker.run())

    await _until(lambda: sqs.receive_calls >= 1)
    worker.request_stop()
    await asyncio.wait_for(run, timeout=1)  # backoff sleep is interruptible


async def test_retry_later_reschedules_instead_of_deleting() -> None:
    from docintel.worker.handler import RetryLaterError

    class Busy:
        async def handle(self, message: Mapping[str, Any]) -> None:
            raise RetryLaterError("UPSTREAM_UNAVAILABLE", delay_s=60)

    sqs = FakeSQS([[_msg(1)]])
    worker = _worker(sqs, Busy())
    run = asyncio.create_task(worker.run())
    await _until(lambda: sqs.visibility == [("r1", 60)])
    worker.request_stop()
    await asyncio.wait_for(run, timeout=1)
    assert sqs.deleted == []  # comes back after the backoff, not after the full timeout


async def test_heartbeat_extends_visibility_while_a_job_runs() -> None:
    class Slow:
        async def handle(self, message: Mapping[str, Any]) -> None:
            await asyncio.sleep(2.5)

    sqs = FakeSQS([[_msg(1)]])
    worker = Worker(
        sqs=sqs, queue_url=QUEUE, handler=Slow(), concurrency=1, visibility_timeout_s=3
    )  # heartbeat every 1 s
    run = asyncio.create_task(worker.run())
    await _until(lambda: sqs.deleted == ["r1"], within_s=5)
    worker.request_stop()
    await asyncio.wait_for(run, timeout=1)
    assert len(sqs.visibility) >= 2
    assert all(v == ("r1", 3) for v in sqs.visibility)


def test_async_retry_backoff() -> None:
    from docintel.worker.extraction_handler import retry_delay_s

    assert [retry_delay_s(n) for n in (1, 2, 3, 4, 10)] == [30, 60, 120, 240, 900]
