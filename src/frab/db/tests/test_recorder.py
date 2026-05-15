"""Tests for DbRecorder — DB-backed recorder for Phase 4."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import frab.db.recorder as _recorder_module
from frab.db.models import (
    EquitySnapshot as EquitySnapshotModel,
    Exchange,
    Fill,
    FundingRate,
    Market,
    Position,
    PositionMode,
    PositionStatus,
    Price,
    Signal,
    Strategy,
)
from frab.db.recorder import DbRecorder
from frab.db.session import session_scope
from frab.exchanges.base import FillReport, FundingTick, Leg, Quote, Side
from frab.strategies.base import EquitySnapshot, SignalEvent, TickReport

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reenable_recorder_logger():
    """Alembic's fileConfig (used in test_migrations.py) calls
    logging.config.fileConfig with disable_existing_loggers=True, which sets
    frab.db.recorder.disabled = True.  Re-enable it before every test so that
    caplog can capture WARNING records regardless of test-collection order.
    """
    log = logging.getLogger("frab.db.recorder")
    log.disabled = False
    if log.level == logging.NOTSET:
        log.setLevel(logging.DEBUG)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
_TS2 = datetime(2024, 6, 1, 13, 0, 0, tzinfo=UTC)


def _make_spot_buy(coin: str, qty: float = 0.1, price: float = 30_000.0, fee: float = 0.21) -> FillReport:
    return FillReport(
        coin=coin,
        leg=Leg.SPOT,
        side=Side.BUY,
        ts=_TS,
        qty=qty,
        price=price,
        fee=fee,
        slippage_bps=2.0,
        is_paper=True,
    )


def _make_perp_sell(coin: str, qty: float = 0.1, price: float = 30_010.0, fee: float = 0.075) -> FillReport:
    return FillReport(
        coin=coin,
        leg=Leg.PERP,
        side=Side.SELL,
        ts=_TS,
        qty=qty,
        price=price,
        fee=fee,
        slippage_bps=1.5,
        is_paper=True,
    )


def _make_spot_sell(coin: str, qty: float = 0.1, price: float = 31_000.0, fee: float = 0.21) -> FillReport:
    return FillReport(
        coin=coin,
        leg=Leg.SPOT,
        side=Side.SELL,
        ts=_TS2,
        qty=qty,
        price=price,
        fee=fee,
        slippage_bps=2.0,
        is_paper=True,
    )


def _make_perp_buy(coin: str, qty: float = 0.1, price: float = 31_010.0, fee: float = 0.075) -> FillReport:
    return FillReport(
        coin=coin,
        leg=Leg.PERP,
        side=Side.BUY,
        ts=_TS2,
        qty=qty,
        price=price,
        fee=fee,
        slippage_bps=1.5,
        is_paper=True,
    )


async def _seed_exchange_and_markets(
    session_factory, exchange_name: str = "HL", coins: list[str] | None = None
) -> tuple[int, dict[str, int]]:
    """Seed an Exchange and Markets; return (exchange_id, {coin: market_id})."""
    if coins is None:
        coins = ["BTC", "ETH"]
    async with session_scope(session_factory) as s:
        exc = Exchange(
            name=exchange_name,
            funding_interval_h=8,
            spot_taker_bps=7.0,
            perp_taker_bps=2.5,
        )
        s.add(exc)
        await s.flush()
        markets = {}
        for coin in coins:
            m = Market(exchange_id=exc.id, coin=coin)
            s.add(m)
            await s.flush()
            markets[coin] = m.id
        exc_id = exc.id
    return exc_id, markets


async def _seed_strategy(session_factory) -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(name="strategy_a", version="v1", params_json={"k": 3})
        s.add(strat)
        await s.flush()
        return strat.id


# ---------------------------------------------------------------------------
# prime tests
# ---------------------------------------------------------------------------


async def test_prime_loads_market_map(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC", "ETH"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    assert set(rec._coin_to_market_id.keys()) == {"BTC", "ETH"}
    assert rec._coin_to_market_id["BTC"] == coin_map["BTC"]
    assert rec._coin_to_market_id["ETH"] == coin_map["ETH"]


async def test_prime_loads_open_positions(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC", "ETH"])
    strat_id = await _seed_strategy(session_factory)

    # Seed 1 OPEN + 1 CLOSED position
    async with session_scope(session_factory) as s:
        open_pos = Position(
            strategy_id=strat_id,
            market_id=coin_map["BTC"],
            mode=PositionMode.PAPER,
            status=PositionStatus.OPEN,
            opened_at=_TS,
            spot_units=0.1,
            perp_units=-0.1,
            entry_spot_price=30_000.0,
            entry_perp_price=30_010.0,
        )
        closed_pos = Position(
            strategy_id=strat_id,
            market_id=coin_map["ETH"],
            mode=PositionMode.PAPER,
            status=PositionStatus.CLOSED,
            opened_at=_TS,
            closed_at=_TS2,
            spot_units=1.0,
            perp_units=-1.0,
            entry_spot_price=2_000.0,
            entry_perp_price=2_001.0,
        )
        s.add(open_pos)
        s.add(closed_pos)
        await s.flush()
        open_pos_id = open_pos.id

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    assert "BTC" in rec._open_positions
    assert rec._open_positions["BTC"] == open_pos_id
    assert "ETH" not in rec._open_positions


# ---------------------------------------------------------------------------
# save_quote tests
# ---------------------------------------------------------------------------


async def test_save_quote_persists_price(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    quote = Quote(coin="BTC", ts=_TS, bid=29_990.0, ask=30_010.0, mark=30_000.0, spot=29_995.0)
    await rec.save_quote(quote)

    async with session_scope(session_factory) as s:
        result = await s.execute(select(Price).where(Price.market_id == coin_map["BTC"]))
        row = result.scalar_one()

    # SQLite strips tzinfo; compare naive datetimes
    assert row.ts.replace(tzinfo=None) == _TS.replace(tzinfo=None)
    assert row.mark == 30_000.0
    assert row.spot == 29_995.0
    assert row.bid == 29_990.0
    assert row.ask == 30_010.0


async def test_save_quote_unknown_coin_logged_no_crash(session_factory, caplog):
    exc_id, _ = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    quote = Quote(coin="UNKNOWN", ts=_TS, bid=1.0, ask=1.1, mark=1.05, spot=1.0)

    with caplog.at_level(logging.WARNING, logger="frab.db.recorder"):
        await rec.save_quote(quote)

    assert any("UNKNOWN" in r.message for r in caplog.records)

    async with session_scope(session_factory) as s:
        result = await s.execute(select(Price))
        assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# save_funding tests
# ---------------------------------------------------------------------------


async def test_save_funding_persists_funding_rate(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    tick = FundingTick(coin="BTC", ts=_TS, rate=0.0001, premium=0.00005, annualized_pct=10.95)
    await rec.save_funding(tick)

    async with session_scope(session_factory) as s:
        result = await s.execute(select(FundingRate).where(FundingRate.market_id == coin_map["BTC"]))
        row = result.scalar_one()

    assert row.ts.replace(tzinfo=None) == _TS.replace(tzinfo=None)
    assert row.rate == 0.0001
    assert row.premium == 0.00005
    assert row.annualized_pct == 10.95


async def test_save_funding_unknown_coin_logged_no_crash(session_factory, caplog):
    exc_id, _ = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    tick = FundingTick(coin="UNKNOWN", ts=_TS, rate=0.0001, premium=None, annualized_pct=10.95)
    with caplog.at_level(logging.WARNING, logger="frab.db.recorder"):
        await rec.save_funding(tick)

    assert any("UNKNOWN" in r.message for r in caplog.records)
    async with session_scope(session_factory) as s:
        result = await s.execute(select(FundingRate))
        assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# save_tick_report — signals
# ---------------------------------------------------------------------------


async def test_save_tick_report_signal_unknown_coin_skipped(session_factory, caplog):
    exc_id, _ = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    signals = (
        SignalEvent(coin="UNKNOWN", ts=_TS, signal_value=5.0, regime_pass=True, action="NONE"),
    )
    report = TickReport(ts=_TS, signals=signals, fills=(), opened=(), closed=())

    with caplog.at_level(logging.WARNING, logger="frab.db.recorder"):
        await rec.save_tick_report(report)

    assert any("UNKNOWN" in r.message for r in caplog.records)
    async with session_scope(session_factory) as s:
        result = await s.execute(select(Signal))
        assert result.scalars().all() == []


async def test_save_tick_report_open_missing_fills_skipped(session_factory, caplog):
    """opened=("BTC",) but no matching fills → warning, no Position created."""
    exc_id, _ = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    report = TickReport(
        ts=_TS,
        signals=(),
        fills=(),  # no fills at all
        opened=("BTC",),
        closed=(),
    )

    with caplog.at_level(logging.WARNING, logger="frab.db.recorder"):
        await rec.save_tick_report(report)

    assert any("BTC" in r.message for r in caplog.records)
    async with session_scope(session_factory) as s:
        result = await s.execute(select(Position))
        assert result.scalars().all() == []


async def test_save_tick_report_close_missing_fills_skipped(session_factory, caplog):
    """Position open in cache but no close fills → warning, position remains OPEN."""
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    # Open a position first
    open_report = TickReport(
        ts=_TS,
        signals=(),
        fills=(_make_spot_buy("BTC"), _make_perp_sell("BTC")),
        opened=("BTC",),
        closed=(),
    )
    await rec.save_tick_report(open_report)

    # Attempt to close without providing fills
    close_report = TickReport(
        ts=_TS2,
        signals=(),
        fills=(),  # no fills
        opened=(),
        closed=("BTC",),
    )

    with caplog.at_level(logging.WARNING, logger="frab.db.recorder"):
        await rec.save_tick_report(close_report)

    assert any("BTC" in r.message for r in caplog.records)

    # Position should still be OPEN
    async with session_scope(session_factory) as s:
        result = await s.execute(select(Position).where(Position.strategy_id == strat_id))
        pos = result.scalar_one()
    assert pos.status == PositionStatus.OPEN

    # And restored in cache
    assert "BTC" in rec._open_positions


async def test_save_tick_report_persists_signals(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC", "ETH", "SOL"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    signals = (
        SignalEvent(coin="BTC", ts=_TS, signal_value=15.0, regime_pass=True, action="OPEN"),
        SignalEvent(coin="ETH", ts=_TS, signal_value=5.0, regime_pass=False, action="NONE"),
        SignalEvent(coin="SOL", ts=_TS, signal_value=20.0, regime_pass=True, action="CLOSE"),
    )
    report = TickReport(ts=_TS, signals=signals, fills=(), opened=(), closed=())
    await rec.save_tick_report(report)

    async with session_scope(session_factory) as s:
        result = await s.execute(
            select(Signal).where(Signal.strategy_id == strat_id).order_by(Signal.market_id)
        )
        rows = result.scalars().all()

    assert len(rows) == 3
    actions = {r.signal_value: r.action for r in rows}
    assert actions[15.0] == "OPEN"
    assert actions[5.0] == "NONE"
    assert actions[20.0] == "CLOSE"


async def test_save_tick_report_signal_none_value_stored_as_zero(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    signals = (
        SignalEvent(coin="BTC", ts=_TS, signal_value=None, regime_pass=True, action="NONE"),
    )
    report = TickReport(ts=_TS, signals=signals, fills=(), opened=(), closed=())
    await rec.save_tick_report(report)

    async with session_scope(session_factory) as s:
        result = await s.execute(select(Signal).where(Signal.strategy_id == strat_id))
        row = result.scalar_one()

    assert row.signal_value == 0.0


# ---------------------------------------------------------------------------
# save_tick_report — opens
# ---------------------------------------------------------------------------


async def test_save_tick_report_opens_position_with_fills(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    spot_buy = _make_spot_buy("BTC", qty=0.1, price=30_000.0, fee=0.21)
    perp_sell = _make_perp_sell("BTC", qty=0.1, price=30_010.0, fee=0.075)

    report = TickReport(
        ts=_TS,
        signals=(),
        fills=(spot_buy, perp_sell),
        opened=("BTC",),
        closed=(),
    )
    await rec.save_tick_report(report)

    async with session_scope(session_factory) as s:
        result = await s.execute(
            select(Position).where(Position.strategy_id == strat_id)
        )
        pos = result.scalar_one()

        fills_result = await s.execute(
            select(Fill).where(Fill.position_id == pos.id)
        )
        fills = fills_result.scalars().all()

    assert pos.status == PositionStatus.OPEN
    assert pos.spot_units == 0.1
    assert pos.perp_units == -0.1  # negative — short convention
    assert pos.entry_spot_price == 30_000.0
    assert pos.entry_perp_price == 30_010.0
    assert pos.fees_paid == pytest.approx(0.21 + 0.075)
    assert pos.realized_pnl == 0.0
    assert len(fills) == 2

    legs = {f.leg for f in fills}
    assert legs == {Leg.SPOT, Leg.PERP}

    # Check recorder's in-memory cache updated
    assert "BTC" in rec._open_positions


async def test_save_tick_report_unknown_open_coin_skipped(session_factory, caplog):
    exc_id, _ = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    spot_buy = _make_spot_buy("UNKNOWN")
    perp_sell = _make_perp_sell("UNKNOWN")
    report = TickReport(
        ts=_TS,
        signals=(),
        fills=(spot_buy, perp_sell),
        opened=("UNKNOWN",),
        closed=(),
    )

    with caplog.at_level(logging.WARNING, logger="frab.db.recorder"):
        await rec.save_tick_report(report)

    assert any("UNKNOWN" in r.message for r in caplog.records)

    async with session_scope(session_factory) as s:
        result = await s.execute(select(Position))
        assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# save_tick_report — closes
# ---------------------------------------------------------------------------


async def test_save_tick_report_closes_position(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    # Open position via first tick report
    spot_buy = _make_spot_buy("BTC", qty=0.1, price=30_000.0, fee=0.21)
    perp_sell = _make_perp_sell("BTC", qty=0.1, price=30_010.0, fee=0.075)
    open_report = TickReport(
        ts=_TS,
        signals=(),
        fills=(spot_buy, perp_sell),
        opened=("BTC",),
        closed=(),
    )
    await rec.save_tick_report(open_report)

    # Close position via second tick report
    spot_sell = _make_spot_sell("BTC", qty=0.1, price=31_000.0, fee=0.21)
    perp_buy = _make_perp_buy("BTC", qty=0.1, price=31_010.0, fee=0.075)
    close_report = TickReport(
        ts=_TS2,
        signals=(),
        fills=(spot_sell, perp_buy),
        opened=(),
        closed=("BTC",),
    )
    await rec.save_tick_report(close_report)

    async with session_scope(session_factory) as s:
        result = await s.execute(
            select(Position).where(Position.strategy_id == strat_id)
        )
        pos = result.scalar_one()

        fills_result = await s.execute(
            select(Fill).where(Fill.position_id == pos.id)
        )
        fills = fills_result.scalars().all()

    assert pos.status == PositionStatus.CLOSED
    assert pos.closed_at.replace(tzinfo=None) == _TS2.replace(tzinfo=None)
    assert pos.exit_spot_price == 31_000.0
    assert pos.exit_perp_price == 31_010.0

    # Realized PnL: qty_magnitude * (entry_perp - exit_perp) = 0.1 * (30010 - 31010) = -100
    assert pos.realized_pnl == pytest.approx(0.1 * (30_010.0 - 31_010.0))

    # fees_paid = open fees + close fees
    assert pos.fees_paid == pytest.approx((0.21 + 0.075) + (0.21 + 0.075))

    # 4 fills total (2 open + 2 close) linked to this position
    assert len(fills) == 4

    # coin no longer in open positions cache
    assert "BTC" not in rec._open_positions


async def test_save_tick_report_close_without_open_skipped(session_factory, caplog):
    exc_id, _ = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    spot_sell = _make_spot_sell("BTC")
    perp_buy = _make_perp_buy("BTC")
    report = TickReport(
        ts=_TS2,
        signals=(),
        fills=(spot_sell, perp_buy),
        opened=(),
        closed=("BTC",),
    )

    with caplog.at_level(logging.WARNING, logger="frab.db.recorder"):
        await rec.save_tick_report(report)

    assert any("BTC" in r.message for r in caplog.records)
    # No crash and no position created
    async with session_scope(session_factory) as s:
        result = await s.execute(select(Position))
        assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# save_equity tests
# ---------------------------------------------------------------------------


async def test_save_equity_persists_snapshot(session_factory):
    exc_id, _ = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    snapshot = EquitySnapshot(
        ts=_TS,
        total_equity=10_000.0,
        cash=5_000.0,
        spot_value=3_000.0,
        perp_unrealized=2_000.0,
        perp_realized_cum=500.0,
        funding_cum=100.0,
        fees_cum=25.0,
    )
    await rec.save_equity(snapshot)

    async with session_scope(session_factory) as s:
        result = await s.execute(
            select(EquitySnapshotModel).where(EquitySnapshotModel.strategy_id == strat_id)
        )
        row = result.scalar_one()

    assert row.ts.replace(tzinfo=None) == _TS.replace(tzinfo=None)
    assert row.total_equity == 10_000.0
    assert row.cash == 5_000.0
    assert row.spot_value == 3_000.0
    assert row.perp_unrealized == 2_000.0
    assert row.perp_realized_cum == 500.0
    assert row.funding_cum == 100.0
    assert row.fees_cum == 25.0


# ---------------------------------------------------------------------------
# Idempotency tests (duplicate timeseries rows)
# ---------------------------------------------------------------------------


async def test_save_quote_is_idempotent_on_duplicate_ts(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    quote = Quote(coin="BTC", ts=_TS, bid=29_990.0, ask=30_010.0, mark=30_000.0, spot=29_995.0)
    await rec.save_quote(quote)
    await rec.save_quote(quote)  # second call — same (coin, ts)

    async with session_scope(session_factory) as s:
        result = await s.execute(select(Price).where(Price.market_id == coin_map["BTC"]))
        rows = result.scalars().all()

    assert len(rows) == 1


async def test_save_funding_is_idempotent_on_duplicate_ts(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    tick = FundingTick(coin="BTC", ts=_TS, rate=0.0001, premium=0.00005, annualized_pct=10.95)
    await rec.save_funding(tick)
    await rec.save_funding(tick)  # second call — same (coin, ts)

    async with session_scope(session_factory) as s:
        result = await s.execute(
            select(FundingRate).where(FundingRate.market_id == coin_map["BTC"])
        )
        rows = result.scalars().all()

    assert len(rows) == 1


async def test_save_tick_report_skips_duplicate_signal(session_factory):
    exc_id, coin_map = await _seed_exchange_and_markets(session_factory, coins=["BTC"])
    strat_id = await _seed_strategy(session_factory)

    rec = DbRecorder(session_factory, strategy_id=strat_id, exchange_id=exc_id)
    await rec.prime()

    ts = datetime(2026, 5, 15, 5, 0, 0, tzinfo=UTC)
    report = TickReport(
        ts=ts,
        signals=(SignalEvent(coin="BTC", ts=ts, signal_value=0.05, regime_pass=True, action="NONE"),),
        fills=(),
        opened=(),
        closed=(),
    )
    await rec.save_tick_report(report)
    await rec.save_tick_report(report)  # second call — same signal (strategy, coin, ts)

    async with session_scope(session_factory) as s:
        result = await s.execute(select(Signal).where(Signal.strategy_id == strat_id))
        rows = result.scalars().all()

    assert len(rows) == 1
