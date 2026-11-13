"""Live job events: Postgres NOTIFY (publish) and a single LISTEN connection per API process
(subscribe). ADR-03.

Notifications are only *wake-up signals*. Subscribers always read the actual events from the
``job_events`` table (``id > last_seen``), so a dropped or coalesced notification can never lose an
event, and a client reconnecting with ``Last-Event-ID`` replays exactly what it missed. If the
LISTEN connection is down, subscribers still wake on their heartbeat timeout and poll; live updates
degrade to "every 15 s" instead of failing.

NOTIFY is transactional: it is delivered only if (and when) the inserting transaction commits, so
a subscriber can never be woken for an event it can't yet read.
"""

import asyncio
import contextlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterator

import asyncpg
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.persistence.db import connect_args
from docintel.settings import Settings

log = structlog.get_logger(__name__)

CHANNEL = "job_events"
_RECONNECT_DELAY_S = (1.0, 30.0)


async def notify_job_event(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Queue a wake-up for ``job_id`` subscribers; sent by Postgres on commit."""
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": CHANNEL, "payload": json.dumps({"job_id": str(job_id)})},
    )


class EventBroker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._subscribers: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        # Background: the API must start (and serve sync traffic) even if Postgres is down.
        self._task = asyncio.create_task(self._listen_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    @contextlib.contextmanager
    def subscribe(self, job_id: uuid.UUID) -> Iterator[asyncio.Event]:
        wake = asyncio.Event()
        self._subscribers[str(job_id)].add(wake)
        try:
            yield wake
        finally:
            subs = self._subscribers.get(str(job_id))
            if subs is not None:
                subs.discard(wake)
                if not subs:
                    del self._subscribers[str(job_id)]

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            job_id = json.loads(payload)["job_id"]
        except (ValueError, KeyError, TypeError):
            log.warning("bad_notify_payload")
            return
        for wake in self._subscribers.get(job_id, ()):
            wake.set()

    def _wake_all(self) -> None:
        """After a reconnect we may have missed notifications: make everyone re-read."""
        for subs in self._subscribers.values():
            for wake in subs:
                wake.set()

    async def _listen_forever(self) -> None:
        delay = _RECONNECT_DELAY_S[0]
        while True:
            conn: asyncpg.Connection | None = None
            try:
                s = self._settings
                args = connect_args(s, role="events")
                conn = await asyncpg.connect(
                    host=s.db_host,
                    port=s.db_port,
                    user=s.db_user,
                    password=s.db_password.get_secret_value(),
                    database=s.db_name,
                    ssl=args["ssl"],
                    timeout=args["timeout"],
                    server_settings=args["server_settings"],
                )
                lost = asyncio.Event()
                conn.add_termination_listener(lambda _conn, signal=lost: signal.set())
                await conn.add_listener(CHANNEL, self._on_notify)
                self._connected.set()
                self._wake_all()
                log.info("event_listener_connected")
                delay = _RECONNECT_DELAY_S[0]
                await lost.wait()  # asyncpg calls the termination listener when the socket dies
                log.warning("event_listener_connection_lost")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("event_listener_connect_failed", retry_in_s=delay, exc_info=True)
            finally:
                self._connected.clear()
                if conn is not None and not conn.is_closed():
                    await conn.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_DELAY_S[1])
