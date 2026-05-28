from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator
import asyncio

from frab.db import models as db_models
from frab.db.session import session_scope


@dataclass(frozen=True, slots=True)
class Event:
    ts: datetime
    level: str        # "INFO" | "WARNING" | "ERROR"
    source: str       # "engine" | "strategy" | "executor" | etc.
    kind: str         # "fill" | "open" | "close" | "tick" | etc.
    message: str
    payload_json: dict | None = None


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    async def publish(self, event: Event) -> None:
        # Fan out to all live subscribers. If a subscriber's queue is full,
        # drop and continue (don't block publisher).
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                continue

    @asynccontextmanager
    async def subscribe(self, *, maxsize: int = 100) -> AsyncIterator[asyncio.Queue[Event]]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)


class EventDbSink:
    """Subscribes to an EventBus and persists each event to the events table.

    Run as an asyncio task: `task = asyncio.create_task(sink.run())`.
    Call `await sink.stop()` to drain and stop.
    """

    def __init__(self, session_factory, bus: EventBus, *, queue_maxsize: int = 1000) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._queue_maxsize = queue_maxsize
        self._stop = asyncio.Event()
        self._subscribed: asyncio.Event = asyncio.Event()

    async def run(self) -> None:
        async with self._bus.subscribe(maxsize=self._queue_maxsize) as q:
            self._subscribed.set()
            while not self._stop.is_set():
                try:
                    event = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                await self._persist(event)

    async def _persist(self, event: Event) -> None:
        ts_ms = int(event.ts.timestamp() * 1000)
        async with session_scope(self._session_factory) as session:
            row = db_models.Event(
                ts_ms=ts_ms,
                level=event.level,
                source=event.source,
                kind=event.kind,
                message=event.message,
                payload_json=event.payload_json,
            )
            session.add(row)

    async def wait_until_subscribed(self) -> None:
        """Block until the sink's queue has been registered with the bus."""
        await self._subscribed.wait()

    async def stop(self) -> None:
        self._stop.set()
