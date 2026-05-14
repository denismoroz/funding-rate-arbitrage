"""Unit tests for ORM models: CRUD, unique constraints, defaults, JSON, cascade."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from frab.db.session import session_scope
from frab.db.models import (
    EquitySnapshot,
    Event,
    Exchange,
    Fill,
    FundingRate,
    Market,
    Position,
    Price,
    Signal,
    Strategy,
    now_ms,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_exchange(**kwargs) -> Exchange:
    defaults = dict(name="HL", funding_interval_h=8, spot_taker_bps=7.0, perp_taker_bps=2.5)
    defaults.update(kwargs)
    return Exchange(**defaults)


def make_strategy(**kwargs) -> Strategy:
    defaults = dict(name="strategy_a", version="v1", params_json={"k": 3})
    defaults.update(kwargs)
    return Strategy(**defaults)


# ---------------------------------------------------------------------------
# now_ms helper
# ---------------------------------------------------------------------------

def test_now_ms_positive():
    ts = now_ms()
    assert isinstance(ts, int)
    assert ts > 0


# ---------------------------------------------------------------------------
# Exchange CRUD + defaults
# ---------------------------------------------------------------------------

async def test_exchange_create_and_read(session):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    result = await session.execute(select(Exchange).where(Exchange.name == "HL"))
    row = result.scalar_one()
    assert row.name == "HL"
    assert row.funding_interval_h == 8
    assert row.spot_taker_bps == 7.0
    assert row.perp_taker_bps == 2.5


async def test_exchange_created_at_ms_default(session):
    exc = make_exchange()
    session.add(exc)
    await session.flush()
    assert exc.created_at_ms > 0


async def test_exchange_update(session):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    exc.spot_taker_bps = 5.0
    await session.flush()

    result = await session.execute(select(Exchange).where(Exchange.id == exc.id))
    assert result.scalar_one().spot_taker_bps == 5.0


async def test_exchange_delete(session):
    exc = make_exchange()
    session.add(exc)
    await session.flush()
    exc_id = exc.id

    await session.delete(exc)
    await session.flush()

    result = await session.execute(select(Exchange).where(Exchange.id == exc_id))
    assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Exchange unique constraint
# ---------------------------------------------------------------------------

async def test_exchange_name_unique(session_factory):
    async with session_scope(session_factory) as s:
        s.add(make_exchange(name="DUP"))

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(make_exchange(name="DUP"))


# ---------------------------------------------------------------------------
# Market CRUD + defaults
# ---------------------------------------------------------------------------

async def test_market_defaults(session):
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


async def test_market_unique_constraint(session_factory):
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
# FundingRate CRUD + unique constraint
# ---------------------------------------------------------------------------

async def test_funding_rate_crud(session):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    fr = FundingRate(
        exchange_id=exc.id, coin="BTC", ts_ms=1000, rate=0.0001,
        premium=None, annualized_pct=10.95,
    )
    session.add(fr)
    await session.flush()

    result = await session.execute(select(FundingRate).where(FundingRate.coin == "BTC"))
    row = result.scalar_one()
    assert row.rate == 0.0001
    assert row.premium is None
    assert row.annualized_pct == 10.95


async def test_funding_rate_unique_constraint(session_factory):
    exc_id: int
    async with session_scope(session_factory) as s:
        exc = make_exchange(name="FRDup")
        s.add(exc)
        await s.flush()
        s.add(FundingRate(exchange_id=exc.id, coin="BTC", ts_ms=2000, rate=0.0001, annualized_pct=10.0))
        exc_id = exc.id

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(FundingRate(exchange_id=exc_id, coin="BTC", ts_ms=2000, rate=0.0001, annualized_pct=10.0))


# ---------------------------------------------------------------------------
# Price CRUD + unique constraint
# ---------------------------------------------------------------------------

async def test_price_crud(session):
    exc = make_exchange()
    session.add(exc)
    await session.flush()

    p = Price(exchange_id=exc.id, coin="SOL", ts_ms=3000, mark=150.0)
    session.add(p)
    await session.flush()

    result = await session.execute(select(Price).where(Price.coin == "SOL"))
    row = result.scalar_one()
    assert row.mark == 150.0
    assert row.spot is None


async def test_price_unique_constraint(session_factory):
    exc_id: int
    async with session_scope(session_factory) as s:
        exc = make_exchange(name="PriceDup")
        s.add(exc)
        await s.flush()
        s.add(Price(exchange_id=exc.id, coin="SOL", ts_ms=4000, mark=100.0))
        exc_id = exc.id

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(Price(exchange_id=exc_id, coin="SOL", ts_ms=4000, mark=100.0))


# ---------------------------------------------------------------------------
# Strategy CRUD + defaults + JSON
# ---------------------------------------------------------------------------

async def test_strategy_defaults(session):
    s = make_strategy()
    session.add(s)
    await session.flush()

    assert s.status == "idle"
    assert s.started_at_ms is None
    assert s.stopped_at_ms is None


async def test_strategy_params_json(session):
    params = {"k": 3, "coins": ["BTC", "ETH"], "threshold": 0.5}
    s = Strategy(name="strategy_b", version="v2", params_json=params)
    session.add(s)
    await session.flush()

    result = await session.execute(select(Strategy).where(Strategy.name == "strategy_b"))
    row = result.scalar_one()
    assert isinstance(row.params_json, dict)
    assert row.params_json["coins"] == ["BTC", "ETH"]


async def test_strategy_unique_constraint(session_factory):
    async with session_scope(session_factory) as s:
        s.add(make_strategy(name="strat", version="v1"))

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(make_strategy(name="strat", version="v1"))


async def test_strategy_update_status(session):
    s = make_strategy()
    session.add(s)
    await session.flush()

    s.status = "running"
    s.started_at_ms = now_ms()
    await session.flush()

    result = await session.execute(select(Strategy).where(Strategy.id == s.id))
    assert result.scalar_one().status == "running"


# ---------------------------------------------------------------------------
# Signal CRUD + unique constraint
# ---------------------------------------------------------------------------

async def test_signal_crud(session):
    strat = make_strategy()
    session.add(strat)
    await session.flush()

    sig = Signal(
        strategy_id=strat.id, coin="BTC", ts_ms=5000,
        signal_value=15.0, action="OPEN",
    )
    session.add(sig)
    await session.flush()

    result = await session.execute(select(Signal).where(Signal.coin == "BTC"))
    row = result.scalar_one()
    assert row.action == "OPEN"
    assert row.regime_pass is True


async def test_signal_unique_constraint(session_factory):
    strat_id: int
    async with session_scope(session_factory) as s:
        strat = make_strategy(name="sig_strat", version="v1")
        s.add(strat)
        await s.flush()
        s.add(Signal(strategy_id=strat.id, coin="ETH", ts_ms=6000, signal_value=10.0, action="NONE"))
        strat_id = strat.id

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(Signal(strategy_id=strat_id, coin="ETH", ts_ms=6000, signal_value=10.0, action="NONE"))


# ---------------------------------------------------------------------------
# Position CRUD + defaults
# ---------------------------------------------------------------------------

def make_position(strategy_id: int, **kwargs) -> Position:
    defaults = dict(
        strategy_id=strategy_id, mode="paper", coin="BTC", status="open",
        opened_at_ms=1_000_000, spot_units=0.1, perp_units=0.1,
        entry_spot_price=30000.0, entry_perp_price=30010.0,
    )
    defaults.update(kwargs)
    return Position(**defaults)


async def test_position_defaults(session):
    strat = make_strategy()
    session.add(strat)
    await session.flush()

    pos = make_position(strat.id)
    session.add(pos)
    await session.flush()

    assert pos.realized_pnl == 0.0
    assert pos.funding_collected == 0.0
    assert pos.fees_paid == 0.0
    assert pos.closed_at_ms is None


async def test_position_update(session):
    strat = make_strategy()
    session.add(strat)
    await session.flush()

    pos = make_position(strat.id)
    session.add(pos)
    await session.flush()

    pos.status = "closed"
    pos.closed_at_ms = now_ms()
    pos.realized_pnl = 42.5
    await session.flush()

    result = await session.execute(select(Position).where(Position.id == pos.id))
    row = result.scalar_one()
    assert row.status == "closed"
    assert row.realized_pnl == 42.5


# ---------------------------------------------------------------------------
# Fill CRUD + defaults
# ---------------------------------------------------------------------------

async def test_fill_defaults(session):
    strat = make_strategy()
    session.add(strat)
    await session.flush()

    pos = make_position(strat.id)
    session.add(pos)
    await session.flush()

    fill = Fill(
        position_id=pos.id, ts_ms=7000, leg="spot", side="buy",
        qty=0.1, price=30000.0, fee=0.21, slippage_bps=2.0,
    )
    session.add(fill)
    await session.flush()

    assert fill.is_paper is True


async def test_fill_read(session):
    strat = make_strategy()
    session.add(strat)
    await session.flush()

    pos = make_position(strat.id)
    session.add(pos)
    await session.flush()

    fill = Fill(
        position_id=pos.id, ts_ms=8000, leg="perp", side="sell",
        qty=0.1, price=30010.0, fee=0.075, slippage_bps=1.5, is_paper=False,
    )
    session.add(fill)
    await session.flush()

    result = await session.execute(select(Fill).where(Fill.position_id == pos.id))
    row = result.scalar_one()
    assert row.leg == "perp"
    assert row.is_paper is False


# ---------------------------------------------------------------------------
# EquitySnapshot CRUD
# ---------------------------------------------------------------------------

async def test_equity_snapshot_crud(session):
    strat = make_strategy()
    session.add(strat)
    await session.flush()

    snap = EquitySnapshot(
        strategy_id=strat.id, ts_ms=9000, total_equity=10000.0,
        cash=5000.0, spot_value=3000.0, perp_unrealized=2000.0,
        perp_realized_cum=0.0, funding_cum=50.0, fees_cum=10.0,
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
# Event CRUD + JSON nullable
# ---------------------------------------------------------------------------

async def test_event_crud(session):
    ev = Event(ts_ms=10000, level="INFO", source="strategy_a", kind="position_opened",
               message="BTC opened", payload_json={"size": 0.1})
    session.add(ev)
    await session.flush()

    result = await session.execute(select(Event).where(Event.kind == "position_opened"))
    row = result.scalar_one()
    assert row.level == "INFO"
    assert isinstance(row.payload_json, dict)
    assert row.payload_json["size"] == 0.1


async def test_event_payload_json_nullable(session):
    ev = Event(ts_ms=11000, level="WARN", source="hl_market", kind="api_error",
               message="timeout", payload_json=None)
    session.add(ev)
    await session.flush()

    result = await session.execute(select(Event).where(Event.kind == "api_error"))
    row = result.scalar_one()
    assert row.payload_json is None


# ---------------------------------------------------------------------------
# Foreign-key cascade: delete Exchange cascades to Market
# ---------------------------------------------------------------------------

async def test_cascade_delete_exchange_removes_markets(session):
    exc = make_exchange(name="CascadeX")
    session.add(exc)
    await session.flush()

    session.add(Market(exchange_id=exc.id, coin="BTC"))
    session.add(Market(exchange_id=exc.id, coin="ETH"))
    await session.flush()

    await session.delete(exc)
    await session.flush()

    result = await session.execute(
        select(Market).where(Market.exchange_id == exc.id)
    )
    assert result.scalars().all() == []
