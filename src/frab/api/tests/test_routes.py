"""Tests for frab FastAPI routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.db.models import (
    EquitySnapshot,
    Event,
    Exchange,
    Fill,
    FundingRate,
    Market,
    Position,
    PositionMode,
    PositionStatus,
    Signal,
    Strategy,
)
from frab.db.session import session_scope
from frab.engine.signals import Decision
from frab.exchanges.base import Leg, Side


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _utc(offset_hours: int = 0) -> datetime:
    return datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(hours=offset_hours)


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


async def _seed_exchange_and_market(
    session_factory, *, coin: str = "BTC", exchange_name: str = "HL"
) -> tuple[Exchange, Market]:
    async with session_scope(session_factory) as s:
        exc = Exchange(
            name=exchange_name,
            funding_interval_h=1,
            spot_taker_bps=2.5,
            perp_taker_bps=2.5,
        )
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin=coin)
        s.add(mkt)
        await s.flush()
        exc_id = exc.id
        mkt_id = mkt.id

    async with session_scope(session_factory) as s:
        from sqlalchemy import select
        exc_r = await s.execute(select(Exchange).where(Exchange.id == exc_id))
        mkt_r = await s.execute(select(Market).where(Market.id == mkt_id))
        return exc_r.scalar_one(), mkt_r.scalar_one()


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
    # sorted by id ascending
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
                strategy_id=s1.id,
                ts=_utc(i),
                total_equity=1000.0 + i,
                cash=500.0,
                spot_value=300.0,
                perp_unrealized=0.0,
                perp_realized_cum=0.0,
                funding_cum=0.0,
                fees_cum=0.0,
            ))
        for i in range(2):
            s.add(EquitySnapshot(
                strategy_id=s2.id,
                ts=_utc(i),
                total_equity=2000.0,
                cash=1000.0,
                spot_value=0.0,
                perp_unrealized=0.0,
                perp_realized_cum=0.0,
                funding_cum=0.0,
                fees_cum=0.0,
            ))

        s1_id = s1.id

    resp = await api_client.get(f"/api/equity?strategy_id={s1_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    # ordered by ts asc
    assert data[0]["ts"] < data[1]["ts"] < data[2]["ts"]


async def test_get_equity_filters_by_since_until(api_client, session_factory):
    async with session_scope(session_factory) as s:
        strat = Strategy(name="sq", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()

        for i in range(5):
            s.add(EquitySnapshot(
                strategy_id=strat.id,
                ts=_utc(i),
                total_equity=float(i),
                cash=0.0,
                spot_value=0.0,
                perp_unrealized=0.0,
                perp_realized_cum=0.0,
                funding_cum=0.0,
                fees_cum=0.0,
            ))
        strat_id = strat.id

    since = _utc(1).isoformat()
    until = _utc(3).isoformat()
    resp = await api_client.get(
        "/api/equity",
        params={"strategy_id": strat_id, "since": since, "until": until},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3  # hours 1, 2, 3


# ---------------------------------------------------------------------------
# /api/positions
# ---------------------------------------------------------------------------


async def _make_position(s, *, strategy_id: int, market_id: int, status: PositionStatus) -> Position:
    pos = Position(
        strategy_id=strategy_id,
        market_id=market_id,
        mode=PositionMode.LIVE,
        status=status,
        opened_at=_utc(),
        spot_units=1.0,
        perp_units=1.0,
        entry_spot_price=100.0,
        entry_perp_price=100.0,
    )
    s.add(pos)
    await s.flush()
    return pos


async def test_get_positions_filters_by_status(api_client, session_factory):
    async with session_scope(session_factory) as s:
        strat = Strategy(name="ps", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_ps", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="BTC")
        s.add(mkt)
        await s.flush()

        await _make_position(s, strategy_id=strat.id, market_id=mkt.id, status=PositionStatus.OPEN)
        await _make_position(s, strategy_id=strat.id, market_id=mkt.id, status=PositionStatus.CLOSED)

    resp = await api_client.get("/api/positions?status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "open"


async def test_get_positions_includes_fills(api_client, session_factory):
    async with session_scope(session_factory) as s:
        strat = Strategy(name="pf", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_pf", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="ETH")
        s.add(mkt)
        await s.flush()

        pos = await _make_position(s, strategy_id=strat.id, market_id=mkt.id, status=PositionStatus.OPEN)

        for i in range(2):
            s.add(Fill(
                position_id=pos.id,
                ts=_utc(i),
                leg=Leg.SPOT,
                side=Side.BUY,
                qty=1.0,
                price=100.0,
                fee=0.5,
                slippage_bps=1.0,
            ))

    resp = await api_client.get("/api/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert len(data[0]["fills"]) == 2


async def test_get_positions_includes_coin(api_client, session_factory):
    async with session_scope(session_factory) as s:
        strat = Strategy(name="pc", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_pc", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="SOL")
        s.add(mkt)
        await s.flush()

        await _make_position(s, strategy_id=strat.id, market_id=mkt.id, status=PositionStatus.OPEN)

    resp = await api_client.get("/api/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["coin"] == "SOL"


# ---------------------------------------------------------------------------
# /api/signals
# ---------------------------------------------------------------------------


async def test_get_signals_filters_by_coin(api_client, session_factory):
    async with session_scope(session_factory) as s:
        strat = Strategy(name="sg", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_sg", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt_btc = Market(exchange_id=exc.id, coin="BTC")
        mkt_eth = Market(exchange_id=exc.id, coin="ETH")
        s.add_all([mkt_btc, mkt_eth])
        await s.flush()

        s.add(Signal(
            strategy_id=strat.id, market_id=mkt_btc.id,
            ts=_utc(), signal_value=0.1, regime_pass=True, action=Decision.OPEN,
        ))
        s.add(Signal(
            strategy_id=strat.id, market_id=mkt_eth.id,
            ts=_utc(), signal_value=0.2, regime_pass=True, action=Decision.NONE,
        ))

    resp = await api_client.get("/api/signals?coin=BTC")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["coin"] == "BTC"


async def test_get_signals_filters_by_strategy_id(api_client, session_factory):
    async with session_scope(session_factory) as s:
        s1 = Strategy(name="ss1", version="v1", params_json={}, status="idle")
        s2 = Strategy(name="ss2", version="v1", params_json={}, status="idle")
        s.add_all([s1, s2])
        await s.flush()
        exc = Exchange(name="HL_ss", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="BTC")
        s.add(mkt)
        await s.flush()

        s.add(Signal(
            strategy_id=s1.id, market_id=mkt.id,
            ts=_utc(0), signal_value=0.1, regime_pass=True, action=Decision.NONE,
        ))
        s.add(Signal(
            strategy_id=s2.id, market_id=mkt.id,
            ts=_utc(1), signal_value=0.2, regime_pass=True, action=Decision.NONE,
        ))
        s1_id = s1.id

    resp = await api_client.get(f"/api/signals?strategy_id={s1_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["strategy_id"] == s1_id


# ---------------------------------------------------------------------------
# /api/funding
# ---------------------------------------------------------------------------


async def test_get_funding_history_by_coin(api_client, session_factory):
    async with session_scope(session_factory) as s:
        exc = Exchange(name="HL_fr", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="BTC")
        s.add(mkt)
        await s.flush()

        for i in range(3):
            s.add(FundingRate(
                market_id=mkt.id,
                ts=_utc(i),
                rate=0.001 * (i + 1),
                premium=None,
                annualized_pct=8.76 * (i + 1),
            ))

    resp = await api_client.get("/api/funding/BTC")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    # ordered desc by ts
    assert data[0]["ts"] > data[1]["ts"] > data[2]["ts"]
    for row in data:
        assert row["coin"] == "BTC"


async def test_get_funding_history_filters_by_exchange(api_client, session_factory):
    async with session_scope(session_factory) as s:
        exc1 = Exchange(name="HL_fe1", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        exc2 = Exchange(name="Binance_fe2", funding_interval_h=8, spot_taker_bps=1.0, perp_taker_bps=1.0)
        s.add_all([exc1, exc2])
        await s.flush()
        mkt1 = Market(exchange_id=exc1.id, coin="BTC")
        mkt2 = Market(exchange_id=exc2.id, coin="BTC")
        s.add_all([mkt1, mkt2])
        await s.flush()

        s.add(FundingRate(market_id=mkt1.id, ts=_utc(0), rate=0.001, premium=None, annualized_pct=8.76))
        s.add(FundingRate(market_id=mkt1.id, ts=_utc(1), rate=0.002, premium=None, annualized_pct=17.52))
        s.add(FundingRate(market_id=mkt2.id, ts=_utc(0), rate=0.003, premium=None, annualized_pct=26.28))
        exc1_id = exc1.id

    resp = await api_client.get(f"/api/funding/BTC?exchange_id={exc1_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    mkt1_id = mkt1.id
    for row in data:
        assert row["market_id"] == mkt1_id


# ---------------------------------------------------------------------------
# /api/events
# ---------------------------------------------------------------------------


async def test_get_events_filters_by_level(api_client, session_factory):
    async with session_scope(session_factory) as s:
        s.add(Event(ts=_utc(0), level="INFO", source="engine", kind="tick", message="info msg"))
        s.add(Event(ts=_utc(1), level="ERROR", source="engine", kind="error", message="error msg"))

    resp = await api_client.get("/api/events?level=ERROR")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["level"] == "ERROR"
    assert data[0]["message"] == "error msg"


async def test_get_events_filters_by_source(api_client, session_factory):
    async with session_scope(session_factory) as s:
        s.add(Event(ts=_utc(0), level="INFO", source="engine", kind="tick", message="from engine"))
        s.add(Event(ts=_utc(1), level="INFO", source="recorder", kind="save", message="from recorder"))

    resp = await api_client.get("/api/events?source=recorder")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["source"] == "recorder"
    assert data[0]["message"] == "from recorder"


# ---------------------------------------------------------------------------
# Direct function-call tests (bypass ASGI for coverage tracking)
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
    assert result[0].name == "direct_a"


async def test_direct_get_strategy_found(session_factory):
    from frab.api.routes.strategies import get_strategy

    async with session_scope(session_factory) as s:
        strat = Strategy(name="direct_b", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        sid = strat.id

    async with session_scope(session_factory) as session:
        result = await get_strategy(strategy_id=sid, session=session)
    assert result.name == "direct_b"


async def test_direct_get_strategy_not_found(session_factory):
    from fastapi import HTTPException

    from frab.api.routes.strategies import get_strategy

    async with session_scope(session_factory) as session:
        try:
            await get_strategy(strategy_id=99999, session=session)
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
                strategy_id=strat.id, ts=_utc(i),
                total_equity=float(i), cash=0.0, spot_value=0.0,
                perp_unrealized=0.0, perp_realized_cum=0.0,
                funding_cum=0.0, fees_cum=0.0,
            ))
        sid = strat.id

    async with session_scope(session_factory) as session:
        result = await list_equity(strategy_id=sid, since=_utc(1), until=_utc(2), limit=10, session=session)
    assert len(result) == 2


async def test_direct_list_positions(session_factory):
    from frab.api.routes.positions import list_positions

    async with session_scope(session_factory) as s:
        strat = Strategy(name="pos_direct", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_pos_d", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="BTC")
        s.add(mkt)
        await s.flush()
        pos = Position(
            strategy_id=strat.id, market_id=mkt.id,
            mode=PositionMode.LIVE, status=PositionStatus.OPEN,
            opened_at=_utc(), spot_units=1.0, perp_units=1.0,
            entry_spot_price=100.0, entry_perp_price=100.0,
        )
        s.add(pos)
        await s.flush()
        s.add(Fill(
            position_id=pos.id, ts=_utc(), leg=Leg.SPOT, side=Side.BUY,
            qty=1.0, price=100.0, fee=0.5, slippage_bps=1.0,
        ))
        sid = strat.id

    async with session_scope(session_factory) as session:
        result = await list_positions(strategy_id=sid, status=None, limit=10, session=session)
    assert len(result) == 1
    assert result[0].coin == "BTC"
    assert len(result[0].fills) == 1


async def test_direct_list_positions_empty(session_factory):
    from frab.api.routes.positions import list_positions

    async with session_scope(session_factory) as session:
        result = await list_positions(strategy_id=None, status=None, limit=10, session=session)
    assert result == []


async def test_direct_list_signals(session_factory):
    from frab.api.routes.signals import list_signals

    async with session_scope(session_factory) as s:
        strat = Strategy(name="sig_direct", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_sig_d", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="BTC")
        s.add(mkt)
        await s.flush()
        s.add(Signal(
            strategy_id=strat.id, market_id=mkt.id,
            ts=_utc(0), signal_value=0.1, regime_pass=True, action=Decision.OPEN,
        ))
        s.add(Signal(
            strategy_id=strat.id, market_id=mkt.id,
            ts=_utc(1), signal_value=0.2, regime_pass=False, action=Decision.NONE,
        ))
        sid = strat.id

    async with session_scope(session_factory) as session:
        result = await list_signals(
            strategy_id=sid, coin="BTC", since=_utc(1), limit=10, session=session
        )
    assert len(result) == 1
    assert result[0].coin == "BTC"


async def test_direct_list_funding(session_factory):
    from frab.api.routes.funding import list_funding_rates

    async with session_scope(session_factory) as s:
        exc = Exchange(name="HL_fr_d", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="ETH")
        s.add(mkt)
        await s.flush()
        for i in range(3):
            s.add(FundingRate(
                market_id=mkt.id, ts=_utc(i),
                rate=0.001 * (i + 1), premium=0.0005,
                annualized_pct=8.76 * (i + 1),
            ))
        exc_id = exc.id

    async with session_scope(session_factory) as session:
        result = await list_funding_rates(
            coin="ETH", exchange_id=exc_id, since=_utc(1), limit=10, session=session
        )
    assert len(result) == 2
    assert result[0].coin == "ETH"


async def test_direct_list_events(session_factory):
    from frab.api.routes.events import list_events

    async with session_scope(session_factory) as s:
        s.add(Event(ts=_utc(0), level="INFO", source="engine", kind="tick", message="m1"))
        s.add(Event(ts=_utc(1), level="WARN", source="rec", kind="save", message="m2", payload_json={"k": "v"}))

    async with session_scope(session_factory) as session:
        result = await list_events(level=None, source="rec", since=None, limit=10, session=session)
    assert len(result) == 1
    assert result[0].payload_json == {"k": "v"}


async def test_direct_list_events_with_since(session_factory):
    from frab.api.routes.events import list_events

    async with session_scope(session_factory) as s:
        s.add(Event(ts=_utc(0), level="INFO", source="engine", kind="tick", message="old"))
        s.add(Event(ts=_utc(5), level="INFO", source="engine", kind="tick", message="new"))

    async with session_scope(session_factory) as session:
        result = await list_events(level=None, source=None, since=_utc(3), limit=10, session=session)
    assert len(result) == 1
    assert result[0].message == "new"
