"""Tests for GET /api/alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from frab.db.models import (
    Event,
    Exchange,
    Market,
    Position,
    PositionMode,
    PositionStatus,
    Strategy,
)
from frab.db.session import session_scope


def _now() -> datetime:
    return datetime.now(UTC)


async def _seed_strategy_market(session_factory, *, name: str = "strat", coin: str = "BTC") -> tuple[int, int]:
    """Return (strategy_id, market_id)."""
    async with session_scope(session_factory) as s:
        strat = Strategy(name=name, version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name=f"EX_{name}", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin=coin)
        s.add(mkt)
        await s.flush()
        return strat.id, mkt.id


async def _seed_failed_position(
    session_factory,
    *,
    strategy_id: int,
    market_id: int,
    opened_at: datetime | None = None,
) -> int:
    if opened_at is None:
        opened_at = _now()
    async with session_scope(session_factory) as s:
        pos = Position(
            strategy_id=strategy_id,
            market_id=market_id,
            mode=PositionMode.PAPER,
            status=PositionStatus.FAILED,
            opened_at=opened_at,
            spot_units=0.1,
            perp_units=-0.1,
            entry_spot_price=30_000.0,
            entry_perp_price=30_010.0,
        )
        s.add(pos)
        await s.flush()
        return pos.id


async def _seed_event(
    session_factory,
    *,
    kind: str,
    ts: datetime | None = None,
    level: str = "ERROR",
    source: str = "atomic_executor",
    message: str = "test event",
    payload_json: dict | None = None,
) -> int:
    if ts is None:
        ts = _now()
    async with session_scope(session_factory) as s:
        evt = Event(ts=ts, level=level, source=source, kind=kind, message=message, payload_json=payload_json)
        s.add(evt)
        await s.flush()
        return evt.id


# ── Tests ──────────────────────────────────────────────────────────────────────

async def test_empty_db_returns_empty_list(api_client):
    resp = await api_client.get("/api/alerts?strategy_id=1")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_failed_position_returns_failed_position_alert(api_client, session_factory):
    strat_id, mkt_id = await _seed_strategy_market(session_factory, name="fp_strat", coin="BTC")
    pos_id = await _seed_failed_position(session_factory, strategy_id=strat_id, market_id=mkt_id)

    resp = await api_client.get(f"/api/alerts?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    alert = data[0]
    assert alert["type"] == "failed_position"
    assert alert["severity"] == "WARNING"
    assert alert["position_id"] == pos_id
    assert alert["coin"] == "BTC"


async def test_failure_event_within_window_returned(api_client, session_factory):
    strat_id, _ = await _seed_strategy_market(session_factory, name="fw_strat", coin="ETH")
    await _seed_event(
        session_factory,
        kind="paired_open_failed",
        ts=_now() - timedelta(hours=1),
        payload_json={"coin": "BTC", "position_id": 42},
    )

    resp = await api_client.get(f"/api/alerts?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    alert = data[0]
    assert alert["type"] == "event"
    assert alert["coin"] == "BTC"
    assert alert["position_id"] == 42


async def test_old_failure_event_outside_window_excluded(api_client, session_factory):
    strat_id, _ = await _seed_strategy_market(session_factory, name="old_strat", coin="SOL")
    await _seed_event(
        session_factory,
        kind="paired_open_failed",
        ts=_now() - timedelta(hours=48),
    )

    resp = await api_client.get(f"/api/alerts?strategy_id={strat_id}")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_explicit_since_overrides_default(api_client, session_factory):
    strat_id, _ = await _seed_strategy_market(session_factory, name="since_strat", coin="DOGE")
    await _seed_event(
        session_factory,
        kind="retry_exhausted",
        ts=_now() - timedelta(hours=48),
        message="old but included",
    )

    resp = await api_client.get(
        f"/api/alerts?strategy_id={strat_id}&since=2020-01-01T00:00:00Z"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["message"] == "old but included"


async def test_non_failure_event_kinds_excluded(api_client, session_factory):
    strat_id, _ = await _seed_strategy_market(session_factory, name="nf_strat", coin="AVAX")
    for kind in ("tick", "open", "close", "signal", "random_info"):
        await _seed_event(session_factory, kind=kind, ts=_now() - timedelta(hours=1))

    resp = await api_client.get(f"/api/alerts?strategy_id={strat_id}")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_failed_position_always_included_regardless_of_since(api_client, session_factory):
    strat_id, mkt_id = await _seed_strategy_market(session_factory, name="always_strat", coin="MATIC")
    old_opened_at = _now() - timedelta(days=100)
    await _seed_failed_position(
        session_factory,
        strategy_id=strat_id,
        market_id=mkt_id,
        opened_at=old_opened_at,
    )

    # Default since=24h ago — FAILED position is 100 days old but must still appear
    resp = await api_client.get(f"/api/alerts?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["type"] == "failed_position"


async def test_other_strategy_failed_position_excluded(api_client, session_factory):
    strat_id, mkt_id = await _seed_strategy_market(session_factory, name="other_strat", coin="LINK")
    await _seed_failed_position(session_factory, strategy_id=strat_id, market_id=mkt_id)

    resp = await api_client.get("/api/alerts?strategy_id=99999")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_merged_sorted_newest_first(api_client, session_factory):
    now = _now()
    strat_id, mkt_id = await _seed_strategy_market(session_factory, name="sort_strat", coin="SUI")
    # FAILED position opened 5h ago
    await _seed_failed_position(
        session_factory,
        strategy_id=strat_id,
        market_id=mkt_id,
        opened_at=now - timedelta(hours=5),
    )
    # Three events at 1h, 2h, 3h ago
    for hours in (1, 2, 3):
        await _seed_event(
            session_factory,
            kind="stuck_position_state",
            ts=now - timedelta(hours=hours),
        )

    resp = await api_client.get(f"/api/alerts?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 4
    for i in range(len(data) - 1):
        assert data[i]["ts"] >= data[i + 1]["ts"]


async def test_response_caps_at_200(api_client, session_factory):
    strat_id, _ = await _seed_strategy_market(session_factory, name="cap_strat", coin="ARB")
    ts_base = _now() - timedelta(hours=1)
    async with session_scope(session_factory) as s:
        for i in range(250):
            s.add(Event(
                ts=ts_base - timedelta(seconds=i),
                level="ERROR",
                source="atomic_executor",
                kind="paired_close_failed",
                message=f"event {i}",
                payload_json=None,
            ))

    resp = await api_client.get(f"/api/alerts?strategy_id={strat_id}")
    assert resp.status_code == 200
    assert len(resp.json()) == 200
