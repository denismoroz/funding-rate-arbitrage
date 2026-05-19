"""Tests for FeeReconciler."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event, select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import (
    Base,
    Exchange,
    Fill,
    Market,
    Position,
    PositionMode,
    PositionStatus,
    Strategy,
)
from frab.db.session import make_session_factory, session_scope
from frab.engine.fee_reconciler import FeeReconciler, ReconcileMatchReport
from frab.events.bus import Event, EventBus
from frab.exchanges.base import Leg, Side, UserFill

# Use a recent-ish anchor so it falls within the 24h lookback window when
# clock_fn is set to _T0 + 25h (i.e. the "now" used by the reconciler is
# slightly after _T0 so the lookback includes _T0).
_T0 = datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC)
# The "now" seen by the reconciler — 30 minutes after fills, within 24h window.
_NOW = _T0 + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# DB fixtures (in-memory aiosqlite, same pattern as other engine tests)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_base(session_factory, *, coin: str = "BTC") -> tuple[int, int, int]:
    """Insert Exchange, Market, Strategy. Return (exchange_id, market_id, strategy_id)."""
    async with session_scope(session_factory) as s:
        exc = Exchange(
            name=f"HL_{coin}",
            funding_interval_h=8,
            spot_taker_bps=7.0,
            perp_taker_bps=2.5,
        )
        s.add(exc)
        await s.flush()

        mkt = Market(exchange_id=exc.id, coin=coin)
        s.add(mkt)
        await s.flush()

        strat = Strategy(name="test_strat", version="v1", params_json={})
        s.add(strat)
        await s.flush()

        return exc.id, mkt.id, strat.id


async def _seed_position_and_fill(
    session_factory,
    *,
    market_id: int,
    strategy_id: int,
    fill_ts: datetime,
    fill_leg: Leg,
    fill_side: Side,
    fill_qty: float,
    fill_price: float = 50000.0,
    fill_fee: float = 0.0,
    is_paper: bool = False,
) -> tuple[int, int]:
    """Insert Position + Fill. Return (position_id, fill_id)."""
    async with session_scope(session_factory) as s:
        pos = Position(
            strategy_id=strategy_id,
            market_id=market_id,
            mode=PositionMode.LIVE,
            status=PositionStatus.OPEN,
            opened_at=fill_ts,
            spot_units=fill_qty if fill_leg == Leg.SPOT else 0.0,
            perp_units=-fill_qty if fill_leg == Leg.PERP else 0.0,
            entry_spot_price=fill_price if fill_leg == Leg.SPOT else 0.0,
            entry_perp_price=fill_price if fill_leg == Leg.PERP else 0.0,
            fees_paid=fill_fee,
        )
        s.add(pos)
        await s.flush()

        fill = Fill(
            position_id=pos.id,
            ts=fill_ts,
            leg=fill_leg,
            side=fill_side,
            qty=fill_qty,
            price=fill_price,
            fee=fill_fee,
            slippage_bps=0.0,
            is_paper=is_paper,
        )
        s.add(fill)
        await s.flush()

        return pos.id, fill.id


def _hl_fill(
    coin: str,
    ts: datetime,
    leg: Leg,
    side: Side,
    qty: float,
    price: float = 50000.0,
    fee: float = 1.0,
    fee_token: str = "USDC",
    hl_oid: int = 100,
    hl_tid: int = 200,
) -> UserFill:
    return UserFill(
        coin=coin,
        ts=ts,
        leg=leg,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        fee_token=fee_token,
        hl_oid=hl_oid,
        hl_tid=hl_tid,
    )


def _make_reconciler(
    session_factory,
    market_data,
    bus: EventBus,
    *,
    now: datetime = _NOW,
) -> FeeReconciler:
    return FeeReconciler(
        session_factory=session_factory,
        market_data=market_data,
        user_address="0xABCD",
        bus=bus,
        lookback_hours=24,
        clock_fn=lambda: now,
    )


# ---------------------------------------------------------------------------
# Test 1: Happy path — 2 unmatched HL fills, 2 local fills with fee=0
# ---------------------------------------------------------------------------


async def test_happy_path_two_fills_matched(session_factory, mocker):
    """2 HL fills (spot BUY + perp SELL) match 2 local fills with fee=0."""
    bus = EventBus()

    _, btc_market_id, strat_id = await _seed_base(session_factory, coin="BTC")

    spot_ts = _T0
    perp_ts = _T0 + timedelta(seconds=1)

    spot_pos_id, spot_fill_id = await _seed_position_and_fill(
        session_factory,
        market_id=btc_market_id,
        strategy_id=strat_id,
        fill_ts=spot_ts,
        fill_leg=Leg.SPOT,
        fill_side=Side.BUY,
        fill_qty=0.001,
        fill_price=80000.0,
        fill_fee=0.0,
    )
    perp_pos_id, perp_fill_id = await _seed_position_and_fill(
        session_factory,
        market_id=btc_market_id,
        strategy_id=strat_id,
        fill_ts=perp_ts,
        fill_leg=Leg.PERP,
        fill_side=Side.SELL,
        fill_qty=0.001,
        fill_price=80001.0,
        fill_fee=0.0,
    )

    hl_fills = [
        # spot BUY — fee in UBTC (asset-denominated), price=80000 → USDC fee = 0.00001 * 80000 = 0.8
        _hl_fill("BTC", spot_ts, Leg.SPOT, Side.BUY, 0.001, price=80000.0, fee=0.00001, fee_token="UBTC"),
        # perp SELL — fee in USDC
        _hl_fill("BTC", perp_ts, Leg.PERP, Side.SELL, 0.001, price=80001.0, fee=2.0, fee_token="USDC"),
    ]

    md = mocker.AsyncMock()
    md.fetch_user_fills.return_value = hl_fills

    reconciler = _make_reconciler(session_factory, md, bus)
    report = await reconciler.run_once()

    assert report.matched == 2
    assert report.unmatched_hl == 0

    # Verify DB: fees written
    async with session_scope(session_factory) as s:
        spot_fill = await s.get(Fill, spot_fill_id)
        perp_fill = await s.get(Fill, perp_fill_id)
        spot_pos = await s.get(Position, spot_pos_id)
        perp_pos = await s.get(Position, perp_pos_id)

    # spot BUY: fee_token=UBTC → 0.00001 * 80000 = 0.8 USDC
    assert spot_fill.fee == pytest.approx(0.8)
    # perp SELL: fee_token=USDC → 2.0
    assert perp_fill.fee == pytest.approx(2.0)
    # Position fees_paid recomputed
    assert spot_pos.fees_paid == pytest.approx(0.8)
    assert perp_pos.fees_paid == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Test 2: Idempotency — second run returns skipped_already_set
# ---------------------------------------------------------------------------


async def test_idempotency_second_run_skips(session_factory, mocker):
    """Run twice. Second run sees fee>0 fills — skipped_already_set=2, no double-update."""
    bus = EventBus()

    _, btc_market_id, strat_id = await _seed_base(session_factory, coin="BTC")

    fill_ts = _T0
    pos_id, fill_id = await _seed_position_and_fill(
        session_factory,
        market_id=btc_market_id,
        strategy_id=strat_id,
        fill_ts=fill_ts,
        fill_leg=Leg.PERP,
        fill_side=Side.SELL,
        fill_qty=0.01,
        fill_price=50000.0,
        fill_fee=0.0,
    )

    hl_fills = [
        _hl_fill("BTC", fill_ts, Leg.PERP, Side.SELL, 0.01, fee=5.0, fee_token="USDC"),
    ]

    md = mocker.AsyncMock()
    md.fetch_user_fills.return_value = hl_fills

    reconciler = _make_reconciler(session_factory, md, bus)

    # First run
    report1 = await reconciler.run_once()
    assert report1.matched == 1
    assert report1.skipped_already_set == 0

    # Second run — fill already has fee=5.0
    report2 = await reconciler.run_once()
    assert report2.matched == 0
    assert report2.skipped_already_set == 1
    assert report2.unmatched_hl == 0

    # DB fill still has fee=5.0 (no double-write)
    async with session_scope(session_factory) as s:
        fill = await s.get(Fill, fill_id)
    assert fill.fee == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Test 3: Unmatched HL fill (qty mismatch >0.1%) → warning event published
# ---------------------------------------------------------------------------


async def test_unmatched_hl_fill_publishes_warning(session_factory, mocker):
    """HL qty differs by >0.1% → unmatched_hl=1, WARNING event emitted."""
    bus = EventBus()
    spy = mocker.spy(bus, "publish")

    _, btc_market_id, strat_id = await _seed_base(session_factory, coin="BTC")

    fill_ts = _T0
    await _seed_position_and_fill(
        session_factory,
        market_id=btc_market_id,
        strategy_id=strat_id,
        fill_ts=fill_ts,
        fill_leg=Leg.PERP,
        fill_side=Side.SELL,
        fill_qty=0.01,
        fill_fee=0.0,
    )

    # HL fill has qty 0.015 — >0.1% different from local 0.01
    hl_fills = [
        _hl_fill("BTC", fill_ts, Leg.PERP, Side.SELL, 0.015, fee=7.5, fee_token="USDC"),
    ]

    md = mocker.AsyncMock()
    md.fetch_user_fills.return_value = hl_fills

    reconciler = _make_reconciler(session_factory, md, bus)
    report = await reconciler.run_once()

    assert report.matched == 0
    assert report.unmatched_hl == 1

    all_events = [call.args[0] for call in spy.call_args_list]
    warning_events = [e for e in all_events if e.level == "WARNING"]
    assert len(warning_events) == 1
    assert warning_events[0].kind == "fee_reconcile_unmatched"
    assert warning_events[0].source == "fee_reconcile"


# ---------------------------------------------------------------------------
# Test 4: Spot BUY fee unit conversion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fee_amount, fee_token, price, expected_usdc",
    [
        (0.000001, "UBTC", 80000.0, 0.08),
        (0.5, "USDC", 80000.0, 0.5),    # USDC stays as-is
    ],
)
async def test_spot_buy_fee_conversion(
    session_factory, mocker, fee_amount, fee_token, price, expected_usdc
):
    """Spot BUY: asset-denominated fee → multiply by price to get USDC."""
    bus = EventBus()

    _, btc_market_id, strat_id = await _seed_base(session_factory, coin="BTC")

    fill_ts = _T0
    pos_id, fill_id = await _seed_position_and_fill(
        session_factory,
        market_id=btc_market_id,
        strategy_id=strat_id,
        fill_ts=fill_ts,
        fill_leg=Leg.SPOT,
        fill_side=Side.BUY,
        fill_qty=0.001,
        fill_price=price,
        fill_fee=0.0,
    )

    hl_fills = [
        _hl_fill(
            "BTC", fill_ts, Leg.SPOT, Side.BUY,
            qty=0.001, price=price, fee=fee_amount, fee_token=fee_token,
        )
    ]

    md = mocker.AsyncMock()
    md.fetch_user_fills.return_value = hl_fills

    reconciler = _make_reconciler(session_factory, md, bus)
    report = await reconciler.run_once()

    assert report.matched == 1

    async with session_scope(session_factory) as s:
        fill = await s.get(Fill, fill_id)
    assert fill.fee == pytest.approx(expected_usdc)


# ---------------------------------------------------------------------------
# Test 5: Time-window edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ts_diff_s, should_match",
    [
        (9, True),    # within TS_WINDOW_S=10
        (11, False),  # outside TS_WINDOW_S=10
    ],
)
async def test_time_window_edge(session_factory, mocker, ts_diff_s, should_match):
    """ts_diff 9s → match; 11s → unmatched."""
    bus = EventBus()

    _, btc_market_id, strat_id = await _seed_base(session_factory, coin="BTC")

    local_ts = _T0
    hl_ts = _T0 + timedelta(seconds=ts_diff_s)

    pos_id, fill_id = await _seed_position_and_fill(
        session_factory,
        market_id=btc_market_id,
        strategy_id=strat_id,
        fill_ts=local_ts,
        fill_leg=Leg.PERP,
        fill_side=Side.SELL,
        fill_qty=0.01,
        fill_fee=0.0,
    )

    hl_fills = [
        _hl_fill("BTC", hl_ts, Leg.PERP, Side.SELL, 0.01, fee=3.0, fee_token="USDC"),
    ]

    md = mocker.AsyncMock()
    md.fetch_user_fills.return_value = hl_fills

    reconciler = _make_reconciler(session_factory, md, bus)
    report = await reconciler.run_once()

    if should_match:
        assert report.matched == 1
        assert report.unmatched_hl == 0
    else:
        assert report.matched == 0
        assert report.unmatched_hl == 1


# ---------------------------------------------------------------------------
# Test 6: No candidates in DB — run succeeds, all stats zero
# ---------------------------------------------------------------------------


async def test_no_candidates_in_db(session_factory, mocker):
    """Empty local fills table — run returns zeros, no exception."""
    bus = EventBus()

    hl_fills = [
        _hl_fill("BTC", _T0, Leg.PERP, Side.SELL, 0.01, fee=1.0, fee_token="USDC"),
    ]

    md = mocker.AsyncMock()
    md.fetch_user_fills.return_value = hl_fills

    reconciler = _make_reconciler(session_factory, md, bus)
    report = await reconciler.run_once()

    # No local fills to match → all go to unmatched
    assert report.matched == 0


async def test_empty_hl_fills_succeeds(session_factory, mocker):
    """HL returns empty list — run completes, all stats zero."""
    bus = EventBus()

    md = mocker.AsyncMock()
    md.fetch_user_fills.return_value = []

    reconciler = _make_reconciler(session_factory, md, bus)
    report = await reconciler.run_once()

    assert report == ReconcileMatchReport(
        candidates_seen=0,
        matched=0,
        skipped_already_set=0,
        unmatched_hl=0,
    )
