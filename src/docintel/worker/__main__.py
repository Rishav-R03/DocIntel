"""Worker entrypoint: ``python -m docintel.worker``.

ECS sends SIGTERM, waits ``stopTimeout`` (30 s in the task definition), then SIGKILLs. The drain
timeout (25 s default) is set below that so in-flight jobs finish or are released cleanly first.
"""

import asyncio
import signal
from collections.abc import Callable

import structlog

from docintel.aws import client_kwargs, create_session
from docintel.logging import configure_logging
from docintel.settings import get_settings
from docintel.worker.handler import LoggingHandler
from docintel.worker.runner import Worker

log = structlog.get_logger(__name__)


def _install_signal_handlers(callback: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, callback)
        except NotImplementedError:  # Windows dev machines
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(callback))


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    session = create_session(settings)
    async with session.client("sqs", **client_kwargs(settings)) as sqs:
        worker = Worker(
            sqs=sqs,
            queue_url=settings.sqs_jobs_url,
            handler=LoggingHandler(),
            concurrency=settings.worker_concurrency,
            wait_time_s=settings.sqs_wait_time_s,
            drain_timeout_s=settings.worker_drain_timeout_s,
        )
        _install_signal_handlers(worker.request_stop)
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
