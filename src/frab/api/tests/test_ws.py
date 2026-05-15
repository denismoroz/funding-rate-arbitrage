"""Tests for the WebSocket /ws/live endpoint."""
from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from frab.api.app import create_app
from frab.db.session import init_db, make_session_factory
from frab.events.bus import Event, EventBus


def _make_event(**overrides) -> Event:
    defaults = dict(
        ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        level="INFO",
        source="engine",
        kind="tick",
        message="hello",
        payload_json={"k": 1},
    )
    defaults.update(overrides)
    return Event(**defaults)


def _has_ws_route(app, path: str) -> bool:
    return any(
        isinstance(r, APIWebSocketRoute) and r.path == path
        for r in app.routes
    )


@pytest_asyncio.fixture
async def sync_session_factory():
    """Session factory usable from sync TestClient threads (separate engine per test)."""
    from sqlalchemy import event as sa_event

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    sa_event.listen(engine.sync_engine, "connect", _enable_fks)
    await init_db(engine)
    yield make_session_factory(engine)
    await engine.dispose()


def test_ws_route_not_registered_without_bus(sync_session_factory):
    app = create_app(sync_session_factory)
    assert app.state.event_bus is None
    assert not _has_ws_route(app, "/ws/live")


def test_ws_route_registered_with_bus(sync_session_factory):
    bus = EventBus()
    app = create_app(sync_session_factory, event_bus=bus)
    assert app.state.event_bus is bus
    assert _has_ws_route(app, "/ws/live")


def test_ws_receives_published_event(sync_session_factory):
    bus = EventBus()
    app = create_app(sync_session_factory, event_bus=bus)
    event = _make_event()

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            time.sleep(0.05)  # let the handler subscribe before we publish
            client.portal.call(bus.publish, event)
            data = ws.receive_json()

    assert data["ts"] == event.ts.isoformat()
    assert data["level"] == "INFO"
    assert data["source"] == "engine"
    assert data["kind"] == "tick"
    assert data["message"] == "hello"
    assert data["payload_json"] == {"k": 1}


def test_ws_receives_multiple_events_in_order(sync_session_factory):
    bus = EventBus()
    app = create_app(sync_session_factory, event_bus=bus)

    events = [
        _make_event(
            ts=datetime(2024, 1, 1, 0, 0, i, tzinfo=UTC),
            message=f"event {i}",
        )
        for i in range(3)
    ]

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            time.sleep(0.05)
            for e in events:
                client.portal.call(bus.publish, e)
            received = [ws.receive_json() for _ in range(3)]

    for i, data in enumerate(received):
        assert data["message"] == f"event {i}"


def test_ws_serializes_none_payload(sync_session_factory):
    bus = EventBus()
    app = create_app(sync_session_factory, event_bus=bus)
    event = _make_event(payload_json=None, message="no payload")

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            time.sleep(0.05)
            client.portal.call(bus.publish, event)
            data = ws.receive_json()

    assert data["payload_json"] is None
    assert data["message"] == "no payload"
