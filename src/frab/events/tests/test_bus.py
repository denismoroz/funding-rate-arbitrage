from datetime import datetime, timezone

import asyncio
import pytest

from sqlalchemy import select

from frab.events.bus import Event, EventBus, EventDbSink
import frab.db.models as db_models


def _make_event(**overrides) -> Event:
    defaults = dict(
        ts=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        level="INFO",
        source="engine",
        kind="tick",
        message="test event",
        payload_json=None,
    )
    defaults.update(overrides)
    return Event(**defaults)


async def test_publish_no_subscribers_noop():
    bus = EventBus()
    event = _make_event()
    # Should complete without error even with no subscribers
    await bus.publish(event)


async def test_subscribe_receives_event():
    bus = EventBus()
    event = _make_event(message="hello", level="WARNING", source="strategy", kind="fill")

    async with bus.subscribe() as q:
        await bus.publish(event)
        received = q.get_nowait()

    assert received.ts == event.ts
    assert received.level == "WARNING"
    assert received.source == "strategy"
    assert received.kind == "fill"
    assert received.message == "hello"
    assert received.payload_json is None


async def test_multiple_subscribers_all_receive():
    bus = EventBus()
    event = _make_event(message="broadcast")

    async with bus.subscribe() as q1:
        async with bus.subscribe() as q2:
            async with bus.subscribe() as q3:
                await bus.publish(event)
                r1 = q1.get_nowait()
                r2 = q2.get_nowait()
                r3 = q3.get_nowait()

    assert r1.message == "broadcast"
    assert r2.message == "broadcast"
    assert r3.message == "broadcast"


async def test_unsubscribe_after_context_exit():
    bus = EventBus()
    event = _make_event()

    async with bus.subscribe():
        assert len(bus._subscribers) == 1

    # After context exit, subscriber is removed
    assert len(bus._subscribers) == 0

    # Publishing after unsubscribe should be a noop
    await bus.publish(event)


async def test_publish_to_full_subscriber_does_not_block_others():
    bus = EventBus()

    # Fill subscriber A's queue to capacity (maxsize=1)
    async with bus.subscribe(maxsize=1) as qa:
        async with bus.subscribe(maxsize=100) as qb:
            # Pre-fill qa so it is full
            initial = _make_event(message="initial")
            await bus.publish(initial)
            assert qa.qsize() == 1

            # Now publish another event; qa is full so it gets dropped, qb receives it
            second = _make_event(message="second")
            await bus.publish(second)

            # B received the second event
            assert qb.qsize() == 2
            r = qb.get_nowait()
            assert r.message == "initial"
            r2 = qb.get_nowait()
            assert r2.message == "second"

            # A still has 1 item (the initial), second was dropped
            assert qa.qsize() == 1
            ra = qa.get_nowait()
            assert ra.message == "initial"


async def test_db_sink_persists_event(session_factory):
    bus = EventBus()
    sink = EventDbSink(session_factory, bus)
    task = asyncio.create_task(sink.run())
    # Yield to let the sink task run and subscribe before publishing
    await asyncio.sleep(0)

    payload = {"price": 42.0, "qty": 1}
    event = _make_event(
        ts=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        level="INFO",
        source="executor",
        kind="fill",
        message="order filled",
        payload_json=payload,
    )
    await bus.publish(event)

    # Give the sink time to process
    await asyncio.sleep(0.1)
    await sink.stop()
    await task

    async with session_factory() as session:
        result = await session.execute(select(db_models.Event))
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    expected_ts_ms = int(event.ts.timestamp() * 1000)
    assert row.ts_ms == expected_ts_ms
    assert row.level == "INFO"
    assert row.source == "executor"
    assert row.kind == "fill"
    assert row.message == "order filled"
    assert row.payload_json == payload


async def test_db_sink_persists_multiple_events_in_order(session_factory):
    bus = EventBus()
    sink = EventDbSink(session_factory, bus)
    task = asyncio.create_task(sink.run())
    # Yield to let the sink task run and subscribe before publishing
    await asyncio.sleep(0)

    events = [
        _make_event(
            ts=datetime(2024, 1, 1, 0, 0, i, tzinfo=timezone.utc),
            message=f"event {i}",
            kind="tick",
        )
        for i in range(3)
    ]
    for e in events:
        await bus.publish(e)

    await asyncio.sleep(0.2)
    await sink.stop()
    await task

    async with session_factory() as session:
        result = await session.execute(
            select(db_models.Event).order_by(db_models.Event.ts_ms)
        )
        rows = result.scalars().all()

    assert len(rows) == 3
    for i, row in enumerate(rows):
        assert row.message == f"event {i}"
        expected_ts_ms = int(events[i].ts.timestamp() * 1000)
        assert row.ts_ms == expected_ts_ms


async def test_db_sink_handles_none_payload(session_factory):
    bus = EventBus()
    sink = EventDbSink(session_factory, bus)
    task = asyncio.create_task(sink.run())
    # Yield to let the sink task run and subscribe before publishing
    await asyncio.sleep(0)

    event = _make_event(payload_json=None, message="no payload")
    await bus.publish(event)

    await asyncio.sleep(0.1)
    await sink.stop()
    await task

    async with session_factory() as session:
        result = await session.execute(select(db_models.Event))
        rows = result.scalars().all()

    assert len(rows) == 1
    assert rows[0].payload_json is None
    assert rows[0].message == "no payload"


async def test_sink_wait_until_subscribed_unblocks_after_subscribe(session_factory):
    """wait_until_subscribed blocks until run() has registered with the bus."""
    bus = EventBus()
    sink = EventDbSink(session_factory, bus)

    # Before run() starts, no one is subscribed.
    assert len(bus._subscribers) == 0

    task = asyncio.create_task(sink.run())
    try:
        # wait_until_subscribed must resolve once run() enters the bus.subscribe context.
        await asyncio.wait_for(sink.wait_until_subscribed(), timeout=1.0)
        assert len(bus._subscribers) == 1
    finally:
        await sink.stop()
        await asyncio.wait_for(task, timeout=2.0)
