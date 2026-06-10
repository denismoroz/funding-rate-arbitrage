"""Unit tests for new ORM models: CRUD, unique constraints, JSON, cascade."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from frab.db.session import session_scope
from frab.db.models import (
    EquitySnapshot,
    Event,
    Exchange,
    FarbPosition,
    Fill,
    FundingAccrual,
    FundingRate,
    Market,
    Position,
    Price,
    Strategy,
    WalletSnapshot,
)
from frab.domain.enums import FarbState, Instrument, PositionStatus, Side

_NOW_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _ms(offset_hours: int = 0) -> int:
    return _NOW_MS + offset_hours * 3600 * 1000


# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------

async def test_exchange_create_and_read(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    result = await session.execute(select(Exchange).where(Exchange.name == "HL"))
    row = result.scalar_one()
    assert row.name == "HL"
    assert row.funding_interval_h == 8
    assert row.spot_taker_bps == 7.0
    assert row.perp_taker_bps == 2.5


async def test_exchange_name_unique(session_factory, make_exchange):
    async with session_scope(session_factory) as s:
        s.add(make_exchange(name="DUP"))

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(make_exchange(name="DUP"))


async def test_exchange_update(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()
    exc.spot_taker_bps = 5.0
    await session.flush()
    result = await session.execute(select(Exchange).where(Exchange.id == exc.id))
    assert result.scalar_one().spot_taker_bps == 5.0


async def test_cascade_delete_exchange_removes_markets(session, make_exchange):
    exc = make_exchange(name="CascadeX")
    session.add(exc)
    await session.flush()
    session.add(Market(exchange_id=exc.id, coin="BTC"))
    session.add(Market(exchange_id=exc.id, coin="ETH"))
    await session.flush()
    await session.delete(exc)
    await session.flush()
    result = await session.execute(select(Market).where(Market.exchange_id == exc.id))
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------

async def test_market_defaults(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()
    mkt = Market(exchange_id=exc.id, coin="BTC")
    session.add(mkt)
    await session.flush()
    assert mkt.has_spot is True
    assert mkt.has_perp is True
    assert mkt.min_size == 0.0
    assert mkt.tick_size == 0.0


async def test_market_unique_constraint(session_factory, make_exchange):
    exc_id: int
    async with session_scope(session_factory) as s:
        exc = make_exchange(name="MktDup")
        s.add(exc)
        await s.flush()
        s.add(Market(exchange_id=exc.id, coin="ETH"))
        exc_id = exc.id

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(Market(exchange_id=exc_id, coin="ETH"))


# ---------------------------------------------------------------------------
# FundingRate
# ---------------------------------------------------------------------------

async def test_funding_rate_crud(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    fr = FundingRate(
        exchange_id=exc.id, coin="BTC", ts_ms=_ms(0),
        rate=0.0001, premium=None, annualized_pct=10.95,
    )
    session.add(fr)
    await session.flush()

    result = await session.execute(
        select(FundingRate).where(FundingRate.exchange_id == exc.id, FundingRate.coin == "BTC")
    )
    row = result.scalar_one()
    assert row.rate == 0.0001
    assert row.premium is None
    assert row.annualized_pct == 10.95


async def test_funding_rate_unique_constraint(session_factory, make_exchange):
    exc_id: int
    async with session_scope(session_factory) as s:
        exc = make_exchange(name="FRDup")
        s.add(exc)
        await s.flush()
        s.add(FundingRate(exchange_id=exc.id, coin="BTC", ts_ms=_ms(1), rate=0.0001, annualized_pct=10.0))
        exc_id = exc.id

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(FundingRate(exchange_id=exc_id, coin="BTC", ts_ms=_ms(1), rate=0.0001, annualized_pct=10.0))


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------

async def test_price_crud(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    p = Price(exchange_id=exc.id, coin="SOL", ts_ms=_ms(3), mark=150.0)
    session.add(p)
    await session.flush()

    result = await session.execute(
        select(Price).where(Price.exchange_id == exc.id, Price.coin == "SOL")
    )
    row = result.scalar_one()
    assert row.mark == 150.0
    assert row.spot is None


async def test_price_unique_constraint(session_factory, make_exchange):
    exc_id: int
    async with session_scope(session_factory) as s:
        exc = make_exchange(name="PriceDup")
        s.add(exc)
        await s.flush()
        s.add(Price(exchange_id=exc.id, coin="SOL", ts_ms=_ms(4), mark=100.0))
        exc_id = exc.id

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(Price(exchange_id=exc_id, coin="SOL", ts_ms=_ms(4), mark=100.0))


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

async def test_strategy_defaults(session, make_strategy):
    s = make_strategy()
    session.add(s)
    await session.flush()
    assert s.status == "idle"
    assert s.started_at_ms is None
    assert s.stopped_at_ms is None


async def test_strategy_params_json(session):
    params = {"k": 3, "coins": ["BTC", "ETH"]}
    s = Strategy(name="strategy_b", version="v2", params_json=params)
    session.add(s)
    await session.flush()

    result = await session.execute(select(Strategy).where(Strategy.name == "strategy_b"))
    row = result.scalar_one()
    assert isinstance(row.params_json, dict)
    assert row.params_json["coins"] == ["BTC", "ETH"]


# ---------------------------------------------------------------------------
# Position + FarbPosition (circular FK)
# ---------------------------------------------------------------------------

async def test_position_crud(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    pos = Position(
        exchange_id=exc.id, coin="BTC",
        instrument=Instrument.SPOT, side=Side.LONG,
        qty=0.1, entry_price=50000.0,
        opened_at=_ms(0), closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=None,
    )
    session.add(pos)
    await session.flush()

    result = await session.execute(select(Position).where(Position.exchange_id == exc.id))
    row = result.scalar_one()
    assert row.coin == "BTC"
    assert row.instrument == Instrument.SPOT
    assert row.side == Side.LONG
    assert row.qty == 0.1
    assert row.entry_price == 50000.0
    assert row.status == PositionStatus.OPEN


async def test_farb_position_crud(session, make_exchange, make_strategy):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    strat = make_strategy()
    session.add(strat)
    await session.flush()

    # Create positions first, farb references them
    spot_pos = Position(
        exchange_id=exc.id, coin="BTC",
        instrument=Instrument.SPOT, side=Side.LONG,
        qty=0.1, entry_price=50000.0,
        opened_at=_ms(0), closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=None,
    )
    perp_pos = Position(
        exchange_id=exc.id, coin="BTC",
        instrument=Instrument.PERP, side=Side.SHORT,
        qty=0.1, entry_price=50010.0,
        opened_at=_ms(0), closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=None,
    )
    session.add_all([spot_pos, perp_pos])
    await session.flush()

    fp = FarbPosition(
        strategy_id=strat.id,
        coin="BTC",
        state=FarbState.PRE_BREAKEVEN,
        state_data={"min_hold_hours": 12},
        spot_position_id=spot_pos.id,
        perp_position_id=perp_pos.id,
        margin_position_id=None,
        opened_at=_ms(0),
        closed_at=None,
    )
    session.add(fp)
    await session.flush()

    # Link positions back to farb
    spot_pos.farb_position_id = fp.id
    perp_pos.farb_position_id = fp.id
    await session.flush()

    result = await session.execute(
        select(FarbPosition).where(FarbPosition.strategy_id == strat.id)
    )
    row = result.scalar_one()
    assert row.coin == "BTC"
    assert row.state == FarbState.PRE_BREAKEVEN
    assert row.state_data["min_hold_hours"] == 12
    assert row.spot_position_id == spot_pos.id
    assert row.perp_position_id == perp_pos.id


# ---------------------------------------------------------------------------
# Fill + FundingAccrual
# ---------------------------------------------------------------------------

async def test_fill_crud(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    pos = Position(
        exchange_id=exc.id, coin="BTC",
        instrument=Instrument.PERP, side=Side.SHORT,
        qty=0.1, entry_price=50000.0,
        opened_at=_ms(0), closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=None,
    )
    session.add(pos)
    await session.flush()

    fill = Fill(
        position_id=pos.id, ts_ms=_ms(0),
        side="sell", qty=0.1, price=50000.0,
        fee=0.15, slippage_bps=2.0, is_paper=False,
    )
    session.add(fill)
    await session.flush()

    result = await session.execute(select(Fill).where(Fill.position_id == pos.id))
    row = result.scalar_one()
    assert row.side == "sell"
    assert row.qty == 0.1
    assert row.is_paper is False


async def test_funding_accrual_crud(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    pos = Position(
        exchange_id=exc.id, coin="BTC",
        instrument=Instrument.PERP, side=Side.SHORT,
        qty=0.1, entry_price=50000.0,
        opened_at=_ms(0), closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=None,
    )
    session.add(pos)
    await session.flush()

    acc = FundingAccrual(position_id=pos.id, ts_ms=_ms(1), amount=0.25)
    session.add(acc)
    await session.flush()

    result = await session.execute(
        select(FundingAccrual).where(FundingAccrual.position_id == pos.id)
    )
    row = result.scalar_one()
    assert row.amount == 0.25


# ---------------------------------------------------------------------------
# WalletSnapshot
# ---------------------------------------------------------------------------

async def test_wallet_snapshot_crud(session, make_exchange):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    snap = WalletSnapshot(
        exchange_id=exc.id, coin="USDC",
        ts_ms=_ms(0), balance=10000.0, source="api",
    )
    session.add(snap)
    await session.flush()

    result = await session.execute(
        select(WalletSnapshot).where(WalletSnapshot.exchange_id == exc.id)
    )
    row = result.scalar_one()
    assert row.coin == "USDC"
    assert row.balance == 10000.0
    assert row.source == "api"


# ---------------------------------------------------------------------------
# EquitySnapshot
# ---------------------------------------------------------------------------

async def test_equity_snapshot_crud(session, make_strategy):
    strat = make_strategy()
    session.add(strat)
    await session.flush()

    snap = EquitySnapshot(
        strategy_id=strat.id, ts_ms=_ms(0),
        total_equity=10000.0, cash=5000.0, spot_value=3000.0,
        perp_unrealized=2000.0, perp_realized_cum=0.0,
        funding_cum=50.0, fees_cum=10.0,
    )
    session.add(snap)
    await session.flush()

    result = await session.execute(
        select(EquitySnapshot).where(EquitySnapshot.strategy_id == strat.id)
    )
    row = result.scalar_one()
    assert row.total_equity == 10000.0
    assert row.funding_cum == 50.0


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

async def test_event_crud(session):
    ev = Event(
        ts_ms=_ms(0), level="INFO", source="strategy_a",
        kind="position_opened", message="BTC opened",
        payload_json={"size": 0.1},
    )
    session.add(ev)
    await session.flush()

    result = await session.execute(select(Event).where(Event.kind == "position_opened"))
    row = result.scalar_one()
    assert row.level == "INFO"
    assert isinstance(row.payload_json, dict)
    assert row.payload_json["size"] == 0.1


async def test_event_payload_json_nullable(session):
    ev = Event(
        ts_ms=_ms(1), level="WARN", source="hl_market",
        kind="api_error", message="timeout", payload_json=None,
    )
    session.add(ev)
    await session.flush()

    result = await session.execute(select(Event).where(Event.kind == "api_error"))
    row = result.scalar_one()
    assert row.payload_json is None
