"""Tests for FundingReconciler."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event, select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import (
    Base,
    Exchange,
    Market,
    Position,
    PositionMode,
    PositionStatus,
    Strategy,
)
from frab.db.session import make_session_factory, session_scope
from frab.engine.funding_reconciler import FundingReconciler
from frab.events.bus import EventBus
from frab.exchanges.base import FundingPayment

_T0 = datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC)
_NOW = _T0 + timedelta(hours=2)


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    sa_event.listen(eng.sync_engine, "connect", _enable_fks)

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return make_session_factory(engine)


async def _seed(session_factory, *, coin: str = "BTC") -> tuple[int, int]:
    async with session_scope(session_factory) as s:
        exc = Exchange(
            name=f"HL_{coin}",
            funding_interval_h=1,
            spot_taker_bps=7.0,
            perp_taker_bps=2.5,
        )
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin=coin)
        s.add(mkt)
        await s.flush()
        strat = Strategy(name="t", version="v1", params_json={})
        s.add(strat)
        await s.flush()
        return mkt.id, strat.id


async def _seed_position(
    session_factory,
    *,
    market_id: int,
    strategy_id: int,
    opened_at: datetime,
    closed_at: datetime | None = None,
    status: PositionStatus = PositionStatus.OPEN,
    funding_collected: float = 0.0,
) -> int:
    async with session_scope(session_factory) as s:
        pos = Position(
            strategy_id=strategy_id,
            market_id=market_id,
            mode=PositionMode.LIVE,
            status=status,
            opened_at=opened_at,
            closed_at=closed_at,
            spot_units=0.00015,
            perp_units=-0.00015,
            entry_spot_price=80000.0,
            entry_perp_price=80000.0,
            funding_collected=funding_collected,
        )
        s.add(pos)
        await s.flush()
        return pos.id


def _payment(coin: str, ts: datetime, usdc: float, hash_: str = "0xH") -> FundingPayment:
    return FundingPayment(coin=coin, ts=ts, usdc=usdc, szi=-0.00015, rate=1.25e-5, hash=hash_)


def _make(session_factory, market_data, bus, *, portfolio_service=None, strategy_id=None) -> FundingReconciler:
    return FundingReconciler(
        session_factory=session_factory,
        market_data=market_data,
        user_address="0xABCD",
        bus=bus,
        lookback_hours=24,
        clock_fn=lambda: _NOW,
        portfolio_service=portfolio_service,
        strategy_id=strategy_id,
    )


async def _get_funding(session_factory, position_id: int) -> float:
    async with session_scope(session_factory) as s:
        row = await s.execute(select(Position.funding_collected).where(Position.id == position_id))
        return float(row.scalar())


# ---------------------------------------------------------------------------


async def test_happy_path_writes_sum_to_position(session_factory, mocker):
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    pos_id = await _seed_position(
        session_factory, market_id=market_id, strategy_id=strategy_id, opened_at=_T0
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[
        _payment("BTC", _T0 + timedelta(hours=1), 0.0001, "0xA"),
        _payment("BTC", _T0 + timedelta(hours=2) - timedelta(minutes=1), 0.0002, "0xB"),
    ])

    rec = _make(session_factory, md, bus)
    report = await rec.run_once()

    assert report.payments_seen == 2
    assert report.positions_updated == 1
    assert report.unmatched == 0
    assert await _get_funding(session_factory, pos_id) == pytest.approx(0.0003)


async def test_idempotent_on_repeat_run(session_factory, mocker):
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    pos_id = await _seed_position(
        session_factory, market_id=market_id, strategy_id=strategy_id, opened_at=_T0
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[
        _payment("BTC", _T0 + timedelta(hours=1), 0.0005, "0xA"),
    ])

    rec = _make(session_factory, md, bus)
    await rec.run_once()
    await rec.run_once()  # second run — overwrites with same value

    assert await _get_funding(session_factory, pos_id) == pytest.approx(0.0005)


async def test_sign_preserved_negative_payment(session_factory, mocker):
    """Negative usdc (we paid funding) sums correctly with positives."""
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    pos_id = await _seed_position(
        session_factory, market_id=market_id, strategy_id=strategy_id, opened_at=_T0
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[
        _payment("BTC", _T0 + timedelta(hours=1), 0.0010),
        _payment("BTC", _T0 + timedelta(hours=2) - timedelta(minutes=10), -0.0003),
    ])

    rec = _make(session_factory, md, bus)
    await rec.run_once()

    assert await _get_funding(session_factory, pos_id) == pytest.approx(0.0007)


async def test_payment_before_open_excluded(session_factory, mocker):
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    pos_id = await _seed_position(
        session_factory, market_id=market_id, strategy_id=strategy_id, opened_at=_T0
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[
        _payment("BTC", _T0 - timedelta(hours=1), 0.0010),  # before open — ignored
        _payment("BTC", _T0 + timedelta(hours=1), 0.0002),
    ])

    rec = _make(session_factory, md, bus)
    report = await rec.run_once()

    assert await _get_funding(session_factory, pos_id) == pytest.approx(0.0002)
    # Pre-history payments (before earliest opened_at for this coin) are
    # silently excluded — they're legit HL income but unattributable.
    assert report.unmatched == 0


async def test_unmatched_coin_warning_event(session_factory, mocker):
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    await _seed_position(
        session_factory, market_id=market_id, strategy_id=strategy_id, opened_at=_T0
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[
        _payment("ETH", _T0 + timedelta(hours=1), 0.0001),  # no ETH position
    ])

    spy = mocker.spy(bus, "publish")
    rec = _make(session_factory, md, bus)
    report = await rec.run_once()

    assert report.unmatched == 1
    # Both INFO done event and WARNING unmatched event were published
    kinds = {call.args[0].kind for call in spy.call_args_list}
    assert "funding_reconcile_unmatched" in kinds


async def test_strategy_funding_cum_synced(session_factory, mocker):
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    await _seed_position(
        session_factory, market_id=market_id, strategy_id=strategy_id, opened_at=_T0
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[
        _payment("BTC", _T0 + timedelta(hours=1), 0.0009),
    ])

    mock_portfolio_service = mocker.MagicMock()
    mock_portfolio_service.set_funding_cum = mocker.AsyncMock()
    rec = _make(session_factory, md, bus, portfolio_service=mock_portfolio_service, strategy_id=strategy_id)
    await rec.run_once()

    mock_portfolio_service.set_funding_cum.assert_awaited_once()
    arg = mock_portfolio_service.set_funding_cum.call_args.args[0]
    assert arg == pytest.approx(0.0009)


async def test_no_strategy_no_setter_call(session_factory, mocker):
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    await _seed_position(
        session_factory, market_id=market_id, strategy_id=strategy_id, opened_at=_T0
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[
        _payment("BTC", _T0 + timedelta(hours=1), 0.0001),
    ])

    rec = _make(session_factory, md, bus)
    # Should not raise — strategy/strategy_id default to None
    await rec.run_once()


async def test_closed_position_within_lookback_reconciled(session_factory, mocker):
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    closed_pos_id = await _seed_position(
        session_factory,
        market_id=market_id,
        strategy_id=strategy_id,
        opened_at=_T0,
        closed_at=_T0 + timedelta(minutes=90),
        status=PositionStatus.CLOSED,
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[
        _payment("BTC", _T0 + timedelta(hours=1), 0.0004),
    ])

    rec = _make(session_factory, md, bus)
    await rec.run_once()

    assert await _get_funding(session_factory, closed_pos_id) == pytest.approx(0.0004)


async def test_closed_position_outside_lookback_skipped(session_factory, mocker):
    bus = EventBus()
    market_id, strategy_id = await _seed(session_factory, coin="BTC")
    # Closed long before the 24h lookback window relative to _NOW
    stale_pos_id = await _seed_position(
        session_factory,
        market_id=market_id,
        strategy_id=strategy_id,
        opened_at=_T0 - timedelta(days=10),
        closed_at=_T0 - timedelta(days=9),
        status=PositionStatus.CLOSED,
        funding_collected=99.0,
    )

    md = mocker.MagicMock()
    md.fetch_user_funding = mocker.AsyncMock(return_value=[])

    rec = _make(session_factory, md, bus)
    await rec.run_once()

    # Unchanged
    assert await _get_funding(session_factory, stale_pos_id) == pytest.approx(99.0)
