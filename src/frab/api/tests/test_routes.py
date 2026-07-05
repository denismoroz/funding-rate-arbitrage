"""Tests for frab FastAPI routes — updated for new schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.db.models import (
    EquitySnapshot,
    Event,
    Exchange,
    FundingRate,
    Market,
    Strategy,
)
from frab.db.session import session_scope


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ms(offset_hours: int = 0) -> int:
    base = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
    return base + offset_hours * 3600 * 1000


async def _seed_strategy(session_factory, *, name: str = "strat", version: str = "v1") -> Strategy:
    async with session_scope(session_factory) as s:
        strategy = Strategy(name=name, version=version, params_json={}, status="idle")
        s.add(strategy)
        await s.flush()
        sid = strategy.id
    async with session_scope(session_factory) as s:
        from sqlalchemy import select
        result = await s.execute(select(Strategy).where(Strategy.id == sid))
        return result.scalar_one()


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


async def test_healthz_returns_ok(api_client):
    resp = await api_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /api/strategies
# ---------------------------------------------------------------------------


async def test_get_strategies_empty(api_client):
    resp = await api_client.get("/api/strategies")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_strategies_lists_all(api_client, session_factory):
    async with session_scope(session_factory) as s:
        s.add(Strategy(name="alpha", version="v1", params_json={}, status="idle"))
        s.add(Strategy(name="beta", version="v1", params_json={}, status="idle"))

    resp = await api_client.get("/api/strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = [d["name"] for d in data]
    assert "alpha" in names
    assert "beta" in names
    assert data[0]["id"] < data[1]["id"]


async def test_get_strategy_by_id(api_client, session_factory):
    strat = await _seed_strategy(session_factory, name="my_strat", version="v2")
    resp = await api_client.get(f"/api/strategies/{strat.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "my_strat"
    assert data["version"] == "v2"


async def test_get_strategy_by_id_not_found(api_client):
    resp = await api_client.get("/api/strategies/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/equity
# ---------------------------------------------------------------------------


async def test_get_equity_requires_strategy_id(api_client):
    resp = await api_client.get("/api/equity")
    assert resp.status_code == 422


async def test_get_equity_returns_snapshots_for_strategy(api_client, session_factory):
    async with session_scope(session_factory) as s:
        s1 = Strategy(name="s1", version="v1", params_json={}, status="idle")
        s2 = Strategy(name="s2", version="v1", params_json={}, status="idle")
        s.add_all([s1, s2])
        await s.flush()

        for i in range(3):
            s.add(EquitySnapshot(
                strategy_id=s1.id, ts_ms=_ms(i),
                total_equity=1000.0 + i, cash=500.0, spot_value=300.0,
                perp_unrealized=0.0, perp_realized_cum=0.0,
                funding_cum=0.0, fees_cum=0.0,
            ))
        for i in range(2):
            s.add(EquitySnapshot(
                strategy_id=s2.id, ts_ms=_ms(i),
                total_equity=2000.0, cash=1000.0, spot_value=0.0,
                perp_unrealized=0.0, perp_realized_cum=0.0,
                funding_cum=0.0, fees_cum=0.0,
            ))
        s1_id = s1.id

    resp = await api_client.get(f"/api/equity?strategy_id={s1_id}&limit=3")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
     # ordered by ts_ms asc
    assert data[0]["ts_ms"] < data[1]["ts_ms"] < data[2]["ts_ms"]


async def test_get_equity_limit_returns_most_recent(api_client, session_factory):
     # Insert 10 records total; limit=3 should return the 3 most recent
     # still in ascending ts_ms order.
     async with session_scope(session_factory) as s:
         stg = Strategy(name="s2", version="v1", params_json={}, status="idle")
         s.add(stg)
         await s.flush()
         sid = stg.id
         for i in range(10):
             s.add(EquitySnapshot(
                 strategy_id=sid, ts_ms=_ms(i),
                 total_equity=1000.0 + i, cash=500.0, spot_value=0.0,
                 perp_unrealized=0.0, perp_realized_cum=0.0,
                 funding_cum=0.0, fees_cum=0.0,
              ))

     resp = await api_client.get(f"/api/equity?strategy_id={sid}&limit=3")
     assert resp.status_code == 200
     data = resp.json()
     assert len(data) == 3
      # ascending order within the slice
     assert data[0]["ts_ms"] < data[1]["ts_ms"] < data[2]["ts_ms"]
     # should be the 3 most recent (indices 7, 8, 9), not the oldest (0, 1, 2)
     assert data[0]["ts_ms"] == _ms(7)
     assert data[2]["ts_ms"] == _ms(9)


async def test_get_equity_bucket_ms_downsamples_and_since_ms_bounds(
    api_client, session_factory
):
    # Six per-minute snapshots spanning two hours (3 in each hour). With
    # bucket_ms=1h we expect one row per hour — the latest in each bucket —
    # and since_ms clips the earlier hour away.
    minute = 60_000
    base = _ms(0)  # top of an hour
    async with session_scope(session_factory) as s:
        stg = Strategy(name="s3", version="v1", params_json={}, status="idle")
        s.add(stg)
        await s.flush()
        sid = stg.id
        offsets = [0, minute, 2 * minute,  # hour 0
                   3600_000, 3600_000 + minute, 3600_000 + 2 * minute]  # hour 1
        for i, off in enumerate(offsets):
            s.add(EquitySnapshot(
                strategy_id=sid, ts_ms=base + off,
                total_equity=1000.0 + i, cash=500.0, spot_value=0.0,
                perp_unrealized=0.0, perp_realized_cum=0.0,
                funding_cum=0.0, fees_cum=0.0,
            ))

    resp = await api_client.get(
        f"/api/equity?strategy_id={sid}&bucket_ms=3600000"
    )
    assert resp.status_code == 200
    data = resp.json()
    # One point per hour: the last snapshot in each bucket.
    assert [d["ts_ms"] for d in data] == [base + 2 * minute, base + 3600_000 + 2 * minute]

    # since_ms drops the first hour's bucket entirely.
    resp2 = await api_client.get(
        f"/api/equity?strategy_id={sid}&bucket_ms=3600000&since_ms={base + 3600_000}"
    )
    assert [d["ts_ms"] for d in resp2.json()] == [base + 3600_000 + 2 * minute]


# ---------------------------------------------------------------------------
# /api/positions — stubbed in Step 3
# ---------------------------------------------------------------------------


async def test_get_positions_returns_list(api_client):
    # Route unfrozen in Step 8: returns 200 + empty list when no positions exist
    resp = await api_client.get("/api/positions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# /api/signals — stubbed in Step 3
# ---------------------------------------------------------------------------


async def test_get_signals_returns_empty(api_client):
    resp = await api_client.get("/api/signals")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# /api/funding
# ---------------------------------------------------------------------------


async def test_get_funding_history_by_coin(api_client, session_factory):
    async with session_scope(session_factory) as s:
        exc = Exchange(name="HL_fr", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        exc_id = exc.id

        for i in range(3):
            s.add(FundingRate(
                exchange_id=exc.id, coin="BTC",
                ts_ms=_ms(i),
                rate=0.001 * (i + 1), premium=None,
                annualized_pct=8.76 * (i + 1),
            ))

    # Pass exchange_id explicitly — the default resolver only knows "hyperliquid"
    resp = await api_client.get(f"/api/funding/BTC?exchange_id={exc_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    # ordered desc by ts_ms
    assert data[0]["ts_ms"] > data[1]["ts_ms"] > data[2]["ts_ms"]
    for row in data:
        assert row["coin"] == "BTC"


async def test_get_funding_history_filters_by_exchange(api_client, session_factory):
    async with session_scope(session_factory) as s:
        exc1 = Exchange(name="HL_fe1", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        exc2 = Exchange(name="Binance_fe2", funding_interval_h=8, spot_taker_bps=1.0, perp_taker_bps=1.0)
        s.add_all([exc1, exc2])
        await s.flush()

        s.add(FundingRate(exchange_id=exc1.id, coin="BTC", ts_ms=_ms(0), rate=0.001, annualized_pct=8.76))
        s.add(FundingRate(exchange_id=exc1.id, coin="BTC", ts_ms=_ms(1), rate=0.002, annualized_pct=17.52))
        s.add(FundingRate(exchange_id=exc2.id, coin="BTC", ts_ms=_ms(0), rate=0.003, annualized_pct=26.28))
        exc1_id = exc1.id

    resp = await api_client.get(f"/api/funding/BTC?exchange_id={exc1_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    for row in data:
        assert row["exchange_id"] == exc1_id


# ---------------------------------------------------------------------------
# /api/events
# ---------------------------------------------------------------------------


async def test_get_events_filters_by_level(api_client, session_factory):
    async with session_scope(session_factory) as s:
        s.add(Event(ts_ms=_ms(0), level="INFO", source="engine", kind="tick", message="info msg"))
        s.add(Event(ts_ms=_ms(1), level="ERROR", source="engine", kind="error", message="error msg"))

    resp = await api_client.get("/api/events?level=ERROR")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["level"] == "ERROR"
    assert data[0]["message"] == "error msg"


async def test_get_events_filters_by_source(api_client, session_factory):
    async with session_scope(session_factory) as s:
        s.add(Event(ts_ms=_ms(0), level="INFO", source="engine", kind="tick", message="from engine"))
        s.add(Event(ts_ms=_ms(1), level="INFO", source="recorder", kind="save", message="from recorder"))

    resp = await api_client.get("/api/events?source=recorder")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["source"] == "recorder"
    assert data[0]["message"] == "from recorder"


# ---------------------------------------------------------------------------
# Direct function-call tests
# ---------------------------------------------------------------------------


async def test_direct_list_strategies_empty(session_factory):
    from frab.api.routes.strategies import list_strategies
    async with session_scope(session_factory) as session:
        result = await list_strategies(session=session)
    assert result == []


async def test_direct_list_strategies_returns_data(session_factory):
    from frab.api.routes.strategies import list_strategies
    async with session_scope(session_factory) as s:
        s.add(Strategy(name="direct_a", version="v1", params_json={"x": 1}, status="idle"))
    async with session_scope(session_factory) as session:
        result = await list_strategies(session=session)
    assert len(result) == 1
    assert result[0]["name"] == "direct_a"


async def test_direct_get_strategy_found(session_factory):
    from frab.api.deps import get_strategy_or_404
    async with session_scope(session_factory) as s:
        strat = Strategy(name="direct_b", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        sid = strat.id
    async with session_scope(session_factory) as session:
        result = await get_strategy_or_404(strategy_id=sid, session=session)
    assert result.name == "direct_b"


async def test_direct_get_strategy_not_found(session_factory):
    from fastapi import HTTPException
    from frab.api.deps import get_strategy_or_404
    async with session_scope(session_factory) as session:
        try:
            await get_strategy_or_404(strategy_id=99999, session=session)
            assert False, "should have raised"
        except HTTPException as e:
            assert e.status_code == 404


async def test_direct_list_equity(session_factory):
    from frab.api.routes.equity import list_equity
    async with session_scope(session_factory) as s:
        strat = Strategy(name="eq_direct", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        for i in range(3):
            s.add(EquitySnapshot(
                strategy_id=strat.id, ts_ms=_ms(i),
                total_equity=float(i), cash=0.0, spot_value=0.0,
                perp_unrealized=0.0, perp_realized_cum=0.0,
                funding_cum=0.0, fees_cum=0.0,
            ))
        sid = strat.id
    async with session_scope(session_factory) as session:
        result = await list_equity(strategy_id=sid, limit=10, session=session)
    assert len(result) == 3


async def test_direct_list_funding(session_factory):
    from frab.api.routes.funding import list_funding_rates
    async with session_scope(session_factory) as s:
        exc = Exchange(name="HL_fr_d", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        for i in range(3):
            s.add(FundingRate(
                exchange_id=exc.id, coin="ETH",
                ts_ms=_ms(i), rate=0.001 * (i + 1),
                premium=0.0005, annualized_pct=8.76 * (i + 1),
            ))
        exc_id = exc.id
    async with session_scope(session_factory) as session:
        result = await list_funding_rates(coin="ETH", exchange_id=exc_id, limit=10, session=session)
    assert len(result) == 3
    assert result[0]["coin"] == "ETH"


async def test_direct_list_events(session_factory):
    from frab.api.routes.events import list_events
    async with session_scope(session_factory) as s:
        s.add(Event(ts_ms=_ms(0), level="INFO", source="engine", kind="tick", message="m1"))
        s.add(Event(ts_ms=_ms(1), level="WARN", source="rec", kind="save", message="m2", payload_json={"k": "v"}))
    async with session_scope(session_factory) as session:
        result = await list_events(level=None, source="rec", limit=10, session=session)
    assert len(result) == 1
    assert result[0]["payload_json"] == {"k": "v"}
