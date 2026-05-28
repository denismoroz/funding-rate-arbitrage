"""Tests for PaperExchange — simulated exchange implementation."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import (
    Fill as DBFill,
    FundingRate as DBFundingRate,
    Position as DBPosition,
    WalletSnapshot as DBWalletSnapshot,
)
from frab.db.session import init_db, make_session_factory, session_scope
from frab.domain import Instrument, PositionStatus, Side
from frab.exchanges.paper import PaperExchange
from frab.exchanges.protocol import Exchange, OpenRequest, Quote, FundingTick, WalletKind


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    from sqlalchemy import event

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(eng.sync_engine, "connect", _enable_fks)
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return make_session_factory(engine)


def _make_upstream_mock(bid: float = 99.0, ask: float = 101.0, mark: float = 100.0):
    """Return a mock upstream Exchange that returns deterministic quotes."""
    upstream = MagicMock(spec=Exchange)
    upstream.get_quote = AsyncMock(return_value=Quote(
        coin="BTC",
        mark=mark,
        spot=None,
        bid=bid,
        ask=ask,
        ts_ms=1_700_000_000_000,
    ))
    upstream.get_funding_rate = AsyncMock(return_value=FundingTick(
        coin="BTC",
        ts_ms=1_700_000_000_000,
        rate=0.0001,
        premium=0.0,
        annualized_pct=0.876,
    ))
    upstream.get_meta = AsyncMock(return_value=[])
    return upstream


def _make_paper(session_factory, upstream=None, fee_bps_spot=7.0, fee_bps_perp=3.5, extra_slip_bps=2.0):
    if upstream is None:
        upstream = _make_upstream_mock()
    return PaperExchange(
        upstream=upstream,
        session_factory=session_factory,
        fee_bps_spot=fee_bps_spot,
        fee_bps_perp=fee_bps_perp,
        extra_slip_bps=extra_slip_bps,
    )


# ---------------------------------------------------------------------------
# 1. open_position SPOT LONG: writes Position + Fill with correct slippage/fee
# ---------------------------------------------------------------------------

async def test_open_position_spot_long_writes_position_and_fill(session_factory):
    """SPOT LONG fill_price = ask * (1 + extra_slip_bps/10000)."""
    upstream = _make_upstream_mock(bid=99.0, ask=101.0, mark=100.0)
    paper = _make_paper(session_factory, upstream=upstream, fee_bps_spot=7.0, extra_slip_bps=2.0)

    req = OpenRequest(coin="BTC", instrument=Instrument.SPOT, side=Side.LONG, qty=0.001)
    pos = await paper.open_position(req)

    expected_fill_price = 101.0 * (1 + 2.0 / 10000)  # ask * (1 + slip)
    expected_fee = 0.001 * expected_fill_price * 7.0 / 10000

    assert pos.id is not None
    assert pos.coin == "BTC"
    assert pos.instrument == Instrument.SPOT
    assert pos.side == Side.LONG
    assert pos.qty == pytest.approx(0.001)
    assert pos.entry_price == pytest.approx(expected_fill_price)
    assert pos.status == PositionStatus.OPEN
    assert pos.exchange_name == "paper"

    # Verify DB
    async with session_scope(session_factory) as s:
        db_pos = await s.get(DBPosition, pos.id)
        assert db_pos is not None
        assert db_pos.entry_price == pytest.approx(expected_fill_price)

        fills_result = await s.execute(
            select(DBFill).where(DBFill.position_id == pos.id)
        )
        fills = fills_result.scalars().all()
        assert len(fills) == 1
        assert fills[0].fee == pytest.approx(expected_fee)
        assert fills[0].is_paper is True
        assert fills[0].slippage_bps == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 2. open_position PERP SHORT: applies correct sign to fill price
# ---------------------------------------------------------------------------

async def test_open_position_perp_short_applies_correct_sign(session_factory):
    """PERP SHORT fill_price = mark * (1 - half_spread/10000 - extra_slip/10000)."""
    upstream = _make_upstream_mock(bid=98.0, ask=102.0, mark=100.0)
    paper = _make_paper(session_factory, upstream=upstream, fee_bps_perp=3.5, extra_slip_bps=2.0)

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    pos = await paper.open_position(req)

    # half_spread_bps = (ask - bid) / mark * 5000 = (102-98)/100 * 5000 = 200
    half_spread_bps = (102.0 - 98.0) / 100.0 * 5000
    expected_fill_price = 100.0 * (1 - half_spread_bps / 10000 - 2.0 / 10000)

    assert pos.entry_price == pytest.approx(expected_fill_price)
    assert pos.instrument == Instrument.PERP
    assert pos.side == Side.SHORT


# ---------------------------------------------------------------------------
# 3. open_position PERP LONG: mark-plus direction
# ---------------------------------------------------------------------------

async def test_open_position_perp_long_applies_correct_sign(session_factory):
    """PERP LONG fill_price = mark * (1 + half_spread/10000 + extra_slip/10000)."""
    upstream = _make_upstream_mock(bid=98.0, ask=102.0, mark=100.0)
    paper = _make_paper(session_factory, upstream=upstream)

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.LONG, qty=0.5)
    pos = await paper.open_position(req)

    half_spread_bps = (102.0 - 98.0) / 100.0 * 5000
    expected_fill_price = 100.0 * (1 + half_spread_bps / 10000 + 2.0 / 10000)

    assert pos.entry_price == pytest.approx(expected_fill_price)


# ---------------------------------------------------------------------------
# 4. open_position COLLATERAL: writes Position + updates wallet_snapshots, no Fill
# ---------------------------------------------------------------------------

async def test_open_position_collateral_writes_position_no_fill(session_factory):
    """COLLATERAL: writes Position(instrument=COLLATERAL, side=NONE), updates wallets."""
    upstream = _make_upstream_mock()
    paper = _make_paper(session_factory, upstream=upstream)

    req = OpenRequest(coin="USDC", instrument=Instrument.COLLATERAL, side=Side.NONE, qty=500.0)
    pos = await paper.open_position(req)

    assert pos.instrument == Instrument.COLLATERAL
    assert pos.side == Side.NONE
    assert pos.qty == pytest.approx(500.0)
    assert pos.entry_price == pytest.approx(1.0)
    assert pos.status == PositionStatus.OPEN

    # No Fill row should exist
    async with session_scope(session_factory) as s:
        fills_result = await s.execute(
            select(DBFill).where(DBFill.position_id == pos.id)
        )
        fills = fills_result.scalars().all()
        assert len(fills) == 0

    # Wallet snapshots should be updated
    perp_bal = await paper.get_wallet("USDC", WalletKind.PERP)
    assert perp_bal == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# 5. close_position: writes closing Fill and updates status to CLOSED
# ---------------------------------------------------------------------------

async def test_close_position_writes_fill_and_updates_status(session_factory):
    """Closing a PERP SHORT writes a closing Fill and sets status=CLOSED."""
    upstream = _make_upstream_mock(bid=98.0, ask=102.0, mark=100.0)
    paper = _make_paper(session_factory, upstream=upstream)

    # Open first
    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    pos = await paper.open_position(req)
    assert pos.status == PositionStatus.OPEN

    # Close
    closed_pos = await paper.close_position(pos)

    assert closed_pos.status == PositionStatus.CLOSED
    assert closed_pos.closed_at is not None

    # Verify DB has 2 fills: opening + closing
    async with session_scope(session_factory) as s:
        fills_result = await s.execute(
            select(DBFill).where(DBFill.position_id == pos.id)
        )
        fills = fills_result.scalars().all()
        assert len(fills) == 2


# ---------------------------------------------------------------------------
# 6. get_open_positions: returns only OPEN status rows
# ---------------------------------------------------------------------------

async def test_get_open_positions_returns_only_open(session_factory):
    upstream = _make_upstream_mock()
    paper = _make_paper(session_factory, upstream=upstream)

    # Open 2 positions
    req1 = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    req2 = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.LONG, qty=0.3)
    pos1 = await paper.open_position(req1)
    pos2 = await paper.open_position(req2)

    # Close one
    await paper.close_position(pos1)

    # get_open_positions should return only pos2
    open_positions = await paper.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].id == pos2.id


# ---------------------------------------------------------------------------
# 7. get_accrued_funding: sums correctly and is idempotent
# ---------------------------------------------------------------------------

async def test_get_accrued_funding_sums_correctly_and_is_idempotent(session_factory):
    """Funding accruals sum correctly; calling twice doesn't double-count."""
    upstream = _make_upstream_mock()
    paper = _make_paper(session_factory, upstream=upstream)

    # Open a PERP SHORT position
    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=1.0)
    pos = await paper.open_position(req)

    # Seed funding_rates table with some ticks.
    # Use timestamps from 2 hours and 1 hour before "opened_at" so they're
    # within [opened_at, now] when get_accrued_funding uses since_ms=opened_at.
    # We override opened_at to be well in the past so the ticks fall in range.
    opened_ms = int(pos.opened_at.timestamp() * 1000)
    # Ticks need to be after opened_at but before now. Since we can't control
    # the exact clock, insert ticks at opened_at - small offset so since_ms
    # includes them... actually we need ticks AFTER opened_at.
    # The simplest fix: use opened_ms itself as the tick (>= condition).
    tick1_ms = opened_ms          # exactly at opened_at, included by >=
    tick2_ms = opened_ms + 1      # 1ms after, also included

    async with session_scope(session_factory) as s:
        # Need exchange_id for paper
        from frab.db.models import Exchange as DBExchange
        exc = await s.scalar(
            select(DBExchange).where(DBExchange.name == "paper")
        )
        assert exc is not None
        exchange_id = exc.id

        s.add(DBFundingRate(
            exchange_id=exchange_id,
            coin="BTC",
            ts_ms=int(tick1_ms),
            rate=0.0001,
            premium=0.0,
            annualized_pct=0.876,
        ))
        s.add(DBFundingRate(
            exchange_id=exchange_id,
            coin="BTC",
            ts_ms=int(tick2_ms),
            rate=0.0002,
            premium=0.0,
            annualized_pct=1.752,
        ))

    # First call
    total1 = await paper.get_accrued_funding(pos)
    # For SHORT: sign=+1, so accrual = qty * rate * 1.0
    # tick1: 1.0 * 0.0001 * 1.0 = 0.0001; tick2: 1.0 * 0.0002 * 1.0 = 0.0002
    expected = 1.0 * 0.0001 + 1.0 * 0.0002
    assert total1 == pytest.approx(expected)

    # Second call (idempotent): same result
    total2 = await paper.get_accrued_funding(pos)
    assert total2 == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 8. transfer: updates both wallet snapshots
# ---------------------------------------------------------------------------

async def test_transfer_updates_both_wallets(session_factory):
    upstream = _make_upstream_mock()
    paper = _make_paper(session_factory, upstream=upstream)

    # Seed initial spot balance
    async with session_scope(session_factory) as s:
        exc_id = await paper._get_or_create_exchange_id(s)
        s.add(DBWalletSnapshot(
            exchange_id=exc_id,
            coin="USDC",
            ts_ms=1000,
            balance=1000.0,
            source="paper_transfer_spot",
        ))

    await paper.transfer("USDC", 300.0, WalletKind.SPOT, WalletKind.PERP)

    spot_bal = await paper.get_wallet("USDC", WalletKind.SPOT)
    perp_bal = await paper.get_wallet("USDC", WalletKind.PERP)

    assert spot_bal == pytest.approx(700.0)  # 1000 - 300
    assert perp_bal == pytest.approx(300.0)   # 0 + 300


# ---------------------------------------------------------------------------
# 9. get_wallet: returns latest snapshot balance (0.0 if none)
# ---------------------------------------------------------------------------

async def test_get_wallet_returns_latest_snapshot(session_factory):
    upstream = _make_upstream_mock()
    paper = _make_paper(session_factory, upstream=upstream)

    # No snapshots yet
    bal = await paper.get_wallet("USDC", WalletKind.SPOT)
    assert bal == pytest.approx(0.0)

    # Add a snapshot
    async with session_scope(session_factory) as s:
        exc_id = await paper._get_or_create_exchange_id(s)
        s.add(DBWalletSnapshot(
            exchange_id=exc_id,
            coin="USDC",
            ts_ms=1000,
            balance=500.0,
            source="paper_transfer_spot",
        ))
        # Newer snapshot
        s.add(DBWalletSnapshot(
            exchange_id=exc_id,
            coin="USDC",
            ts_ms=2000,
            balance=750.0,
            source="paper_transfer_spot",
        ))

    bal = await paper.get_wallet("USDC", WalletKind.SPOT)
    assert bal == pytest.approx(750.0)  # returns latest


# ---------------------------------------------------------------------------
# 10. open_position SPOT SHORT: raises NotImplementedError
# ---------------------------------------------------------------------------

async def test_open_position_spot_short_raises(session_factory):
    upstream = _make_upstream_mock()
    paper = _make_paper(session_factory, upstream=upstream)

    req = OpenRequest(coin="BTC", instrument=Instrument.SPOT, side=Side.SHORT, qty=0.001)
    with pytest.raises(NotImplementedError):
        await paper.open_position(req)


# ---------------------------------------------------------------------------
# 11. PaperExchange lazy exchange_id creation
# ---------------------------------------------------------------------------

async def test_paper_exchange_lazy_creates_exchange_row(session_factory):
    """PaperExchange creates the 'paper' exchange row on first DB write."""
    from frab.db.models import Exchange as DBExchange

    upstream = _make_upstream_mock()
    paper = _make_paper(session_factory, upstream=upstream)

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    await paper.open_position(req)

    async with session_scope(session_factory) as s:
        exc = await s.scalar(select(DBExchange).where(DBExchange.name == "paper"))
        assert exc is not None
        assert exc.name == "paper"


# ---------------------------------------------------------------------------
# 12. get_quote / get_funding_rate / get_meta delegate to upstream
# ---------------------------------------------------------------------------

async def test_read_methods_delegate_to_upstream(session_factory):
    upstream = _make_upstream_mock()
    paper = _make_paper(session_factory, upstream=upstream)

    quote = await paper.get_quote("BTC")
    assert quote.mark == pytest.approx(100.0)
    upstream.get_quote.assert_called_once_with("BTC")

    tick = await paper.get_funding_rate("BTC")
    assert tick.rate == pytest.approx(0.0001)
    upstream.get_funding_rate.assert_called_once_with("BTC")

    meta = await paper.get_meta()
    assert meta == []
    upstream.get_meta.assert_called_once()
