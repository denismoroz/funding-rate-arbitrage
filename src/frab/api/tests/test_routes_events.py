"""Events route tests — kind_prefix filter."""

from __future__ import annotations

from datetime import UTC, datetime

from frab.db.models import Event
from frab.db.session import session_scope


def _ms(offset_hours: int = 0) -> int:
    base = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
    return base + offset_hours * 3600 * 1000


async def test_get_events_filters_by_kind_prefix(api_client, session_factory):
    async with session_scope(session_factory) as s:
        s.add(Event(ts_ms=_ms(0), level="INFO", source="strategy", kind="xsmom.rebalanced", message="rebal"))
        s.add(Event(ts_ms=_ms(1), level="INFO", source="strategy", kind="xsmom.opened", message="opened"))
        s.add(Event(ts_ms=_ms(2), level="INFO", source="engine", kind="tick.completed", message="tick"))
        s.add(Event(ts_ms=_ms(3), level="INFO", source="strategy", kind="farb.opened", message="farb"))

    resp = await api_client.get("/api/events?kind_prefix=xsmom")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all(e["kind"].startswith("xsmom") for e in data)
