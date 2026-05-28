"""Tests for Ledger (Step 6) — stateless equity aggregator.

11 required scenarios + edge cases.
Uses in-memory async SQLite.
"""
from __future__ import annotations

import logging

import pytest

from frab.db.models import (
    FarbPosition as FarbPositionRow,
    Fill as FillRow,
    FundingAccrual as FundingAccrualRow,
    Position as PositionRow,
    WalletSnapshot as WalletSnapshotRow,
    EquitySnapshot as EquitySnapshotRow,
)
from frab.db.session import session_scope
from frab.domain.enums import FarbState, Instrument, PositionStatus, Side
from frab.exchanges.protocol import Quote
from frab.ledger.ledger import EquitySnapshot, Ledger

_NOW_MS = 1704067200000  # 2024-01-01 00:00:00 UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quote(coin: str, mark: float, spot: float | None = None) -> Quote:
    return Quote(
        coin=coin,
        mark=mark,
        spot=spot,
        bid=mark * 0.999,
        ask=mark * 1.001,
        ts_ms=_NOW_MS,
    )


async def _insert_farb_position(
    session_factory,
    strategy_id: int,
    coin: str,
    state: FarbState = FarbState.OPEN,
) -> int:
    """Insert a farb_position row, return its id."""
    async with session_scope(session_factory) as s:
        fp = FarbPositionRow(
            strategy_id=strategy_id,
            coin=coin,
            state=state.value,
            state_data={},
            spot_position_id=None,
            perp_position_id=None,
            margin_position_id=None,
            opened_at=_NOW_MS,
            closed_at=None,
        )
        s.add(fp)
        await s.flush()
        fid = fp.id
    return fid


async def _insert_position(
    session_factory,
    exchange_id: int,
    coin: str,
    instrument: Instrument,
    side: Side,
    qty: float,
    entry_price: float,
    farb_position_id: int | None,
    status: PositionStatus = PositionStatus.OPEN,
    closed_at: int | None = None,
) -> int:
    """Insert a position row, return its id."""
    async with session_scope(session_factory) as s:
        pos = PositionRow(
            exchange_id=exchange_id,
            coin=coin,
            instrument=instrument.value,
            side=side.value,
            qty=qty,
            entry_price=entry_price,
            opened_at=_NOW_MS,
            closed_at=closed_at,
            status=status.value,
            farb_position_id=farb_position_id,
        )
        s.add(pos)
        await s.flush()
        pid = pos.id
    return pid


async def _insert_fill(
    session_factory,
    position_id: int,
    side: Side,
    qty: float,
    price: float,
    fee: float = 0.0,
) -> None:
    async with session_scope(session_factory) as s:
        f = FillRow(
            position_id=position_id,
            ts_ms=_NOW_MS,
            side=side.value,
            qty=qty,
            price=price,
            fee=fee,
            slippage_bps=0.0,
            is_paper=False,
        )
        s.add(f)


async def _insert_funding_accrual(
    session_factory,
    position_id: int,
    amount: float,
) -> None:
    async with session_scope(session_factory) as s:
        fa = FundingAccrualRow(
            position_id=position_id,
            ts_ms=_NOW_MS,
            amount=amount,
        )
        s.add(fa)


async def _insert_wallet_snapshot(
    session_factory,
    exchange_id: int,
    coin: str,
    balance: float,
    ts_ms: int = _NOW_MS,
) -> None:
    async with session_scope(session_factory) as s:
        ws = WalletSnapshotRow(
            exchange_id=exchange_id,
            coin=coin,
            ts_ms=ts_ms,
            balance=balance,
            source="test",
        )
        s.add(ws)


# ---------------------------------------------------------------------------
# Test 1: Empty DB → all-zero snapshot
# ---------------------------------------------------------------------------


async def test_empty_db_returns_zero_snapshot(session_factory, strategy_id):
    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    assert snap.strategy_id == strategy_id
    assert snap.ts_ms > 0
    assert snap.total_equity == 0.0
    assert snap.cash == 0.0
    assert snap.spot_value == 0.0
    assert snap.perp_unrealized == 0.0
    assert snap.perp_realized_cum == 0.0
    assert snap.funding_cum == 0.0
    assert snap.fees_cum == 0.0


# ---------------------------------------------------------------------------
# Test 2: One open SPOT LONG → spot_value populated, perp_unrealized=0
# ---------------------------------------------------------------------------


async def test_spot_long_populates_spot_value(session_factory, strategy_id, exchange_id):
    fid = await _insert_farb_position(session_factory, strategy_id, "BTC")
    await _insert_position(
        session_factory,
        exchange_id=exchange_id,
        coin="BTC",
        instrument=Instrument.SPOT,
        side=Side.LONG,
        qty=1.0,
        entry_price=50_000.0,
        farb_position_id=fid,
    )

    quotes = {"BTC": _quote("BTC", mark=52_000.0, spot=52_500.0)}
    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes=quotes)

    # spot price preferred over mark for SPOT positions
    assert snap.spot_value == pytest.approx(52_500.0)
    assert snap.perp_unrealized == pytest.approx(0.0)
    assert snap.total_equity == pytest.approx(snap.cash + snap.spot_value + snap.perp_unrealized + snap.funding_cum)


# ---------------------------------------------------------------------------
# Test 3: Delta-neutral (1 BTC spot LONG @ 50k + 1 BTC perp SHORT @ 50k),
#         mark moves to 51k → spot_value=51k, perp_unrealized=-1k, net ≈ 0
# ---------------------------------------------------------------------------


async def test_delta_neutral_net_zero(session_factory, strategy_id, exchange_id):
    fid = await _insert_farb_position(session_factory, strategy_id, "BTC")
    # Spot leg
    await _insert_position(
        session_factory,
        exchange_id=exchange_id,
        coin="BTC",
        instrument=Instrument.SPOT,
        side=Side.LONG,
        qty=1.0,
        entry_price=50_000.0,
        farb_position_id=fid,
    )
    # Perp leg
    await _insert_position(
        session_factory,
        exchange_id=exchange_id,
        coin="BTC",
        instrument=Instrument.PERP,
        side=Side.SHORT,
        qty=1.0,
        entry_price=50_000.0,
        farb_position_id=fid,
    )

    new_mark = 51_000.0
    quotes = {"BTC": _quote("BTC", mark=new_mark, spot=new_mark)}
    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes=quotes)

    assert snap.spot_value == pytest.approx(51_000.0)
    # SHORT perp: (entry - mark) * qty = (50k - 51k) * 1 = -1000
    assert snap.perp_unrealized == pytest.approx(-1_000.0)
    # Net change from open: spot gained 1k, perp lost 1k → total change = 0
    net_position_value = snap.spot_value + snap.perp_unrealized
    assert net_position_value == pytest.approx(50_000.0)


# ---------------------------------------------------------------------------
# Test 4: funding_cum sums correctly across multiple farb_positions / accruals
# ---------------------------------------------------------------------------


async def test_funding_cum_sums_across_positions(session_factory, strategy_id, exchange_id):
    fid1 = await _insert_farb_position(session_factory, strategy_id, "BTC")
    fid2 = await _insert_farb_position(session_factory, strategy_id, "ETH")

    pid1 = await _insert_position(
        session_factory, exchange_id, "BTC", Instrument.PERP, Side.SHORT,
        qty=1.0, entry_price=50_000.0, farb_position_id=fid1,
    )
    pid2 = await _insert_position(
        session_factory, exchange_id, "ETH", Instrument.PERP, Side.SHORT,
        qty=10.0, entry_price=3_000.0, farb_position_id=fid2,
    )

    await _insert_funding_accrual(session_factory, pid1, amount=10.0)
    await _insert_funding_accrual(session_factory, pid1, amount=15.0)
    await _insert_funding_accrual(session_factory, pid2, amount=5.0)

    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    assert snap.funding_cum == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Test 5: fees_cum sums open fill + close fill
# ---------------------------------------------------------------------------


async def test_fees_cum_sums_fills(session_factory, strategy_id, exchange_id):
    fid = await _insert_farb_position(session_factory, strategy_id, "BTC")
    pid = await _insert_position(
        session_factory, exchange_id, "BTC", Instrument.PERP, Side.SHORT,
        qty=1.0, entry_price=50_000.0, farb_position_id=fid,
    )
    await _insert_fill(session_factory, pid, Side.SHORT, qty=1.0, price=50_000.0, fee=5.0)
    await _insert_fill(session_factory, pid, Side.LONG, qty=1.0, price=51_000.0, fee=5.1)

    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    assert snap.fees_cum == pytest.approx(10.1)


# ---------------------------------------------------------------------------
# Test 6: perp_realized_cum — open + close a PERP SHORT
# ---------------------------------------------------------------------------


async def test_perp_realized_cum_short(session_factory, strategy_id, exchange_id):
    """Open SHORT @ 50k, close @ 49k → realized = (50k - 49k)*1 - fees = 1000 - fee."""
    fid = await _insert_farb_position(session_factory, strategy_id, "BTC")
    pid = await _insert_position(
        session_factory, exchange_id, "BTC", Instrument.PERP, Side.SHORT,
        qty=1.0, entry_price=50_000.0, farb_position_id=fid,
        status=PositionStatus.CLOSED, closed_at=_NOW_MS + 3600_000,
    )
    # Opening fill (SHORT)
    await _insert_fill(session_factory, pid, Side.SHORT, qty=1.0, price=50_000.0, fee=5.0)
    # Closing fill (LONG — opposite side)
    await _insert_fill(session_factory, pid, Side.LONG, qty=1.0, price=49_000.0, fee=4.9)

    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    # (entry - exit) * qty - close_fees = (50k - 49k) * 1 - 4.9 = 995.1
    assert snap.perp_realized_cum == pytest.approx(995.1)


async def test_perp_realized_cum_long(session_factory, strategy_id, exchange_id):
    """Open LONG @ 48k, close @ 50k → realized = (50k - 48k)*1 - fees."""
    fid = await _insert_farb_position(session_factory, strategy_id, "BTC")
    pid = await _insert_position(
        session_factory, exchange_id, "BTC", Instrument.PERP, Side.LONG,
        qty=1.0, entry_price=48_000.0, farb_position_id=fid,
        status=PositionStatus.CLOSED, closed_at=_NOW_MS + 3600_000,
    )
    await _insert_fill(session_factory, pid, Side.LONG, qty=1.0, price=48_000.0, fee=4.8)
    await _insert_fill(session_factory, pid, Side.SHORT, qty=1.0, price=50_000.0, fee=5.0)

    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    # (exit - entry) * qty - close_fees = (50k - 48k)*1 - 5.0 = 1995.0
    assert snap.perp_realized_cum == pytest.approx(1995.0)


# ---------------------------------------------------------------------------
# Test 7: cash — multiple wallet_snapshots for same (exchange, coin) → latest wins
# ---------------------------------------------------------------------------


async def test_cash_latest_wallet_snapshot_wins(session_factory, strategy_id, exchange_id):
    # Three snapshots for the same (exchange, USDC), different timestamps
    await _insert_wallet_snapshot(session_factory, exchange_id, "USDC", balance=1000.0, ts_ms=_NOW_MS - 2000)
    await _insert_wallet_snapshot(session_factory, exchange_id, "USDC", balance=1500.0, ts_ms=_NOW_MS - 1000)
    await _insert_wallet_snapshot(session_factory, exchange_id, "USDC", balance=2000.0, ts_ms=_NOW_MS)

    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    # Only the latest (2000.0) should count
    assert snap.cash == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# Test 8: cash — USDT and USDC sum; non-stablecoin balances ignored
# ---------------------------------------------------------------------------


async def test_cash_usdt_and_usdc_sum_non_stablecoin_ignored(session_factory, strategy_id, exchange_id):
    await _insert_wallet_snapshot(session_factory, exchange_id, "USDC", balance=1000.0)
    await _insert_wallet_snapshot(session_factory, exchange_id, "USDT", balance=500.0)
    # BTC wallet balance — should be ignored
    await _insert_wallet_snapshot(session_factory, exchange_id, "BTC", balance=0.5)

    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    assert snap.cash == pytest.approx(1500.0)


# ---------------------------------------------------------------------------
# Test 9: Missing quote → contribution = 0, WARN logged
# ---------------------------------------------------------------------------


async def test_missing_quote_contribution_zero_and_warn_logged(
    session_factory, strategy_id, exchange_id, caplog
):
    fid = await _insert_farb_position(session_factory, strategy_id, "BTC")
    await _insert_position(
        session_factory, exchange_id, "BTC", Instrument.SPOT, Side.LONG,
        qty=1.0, entry_price=50_000.0, farb_position_id=fid,
    )

    ledger = Ledger(session_factory)

    with caplog.at_level(logging.WARNING, logger="frab.ledger.ledger"):
        snap = await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    # Contribution is 0
    assert snap.spot_value == pytest.approx(0.0)
    assert snap.total_equity == pytest.approx(0.0)

    # WARN was logged
    assert any(
        "BTC" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    ), f"Expected WARN about missing BTC quote; got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Test 10: COLLATERAL positions are NOT counted in spot_value or perp_unrealized
# ---------------------------------------------------------------------------


async def test_collateral_not_double_counted(session_factory, strategy_id, exchange_id):
    fid = await _insert_farb_position(session_factory, strategy_id, "BTC")
    # COLLATERAL position (value = qty of USDC, already in cash/wallet balance)
    await _insert_position(
        session_factory, exchange_id, "USDC", Instrument.COLLATERAL, Side.NONE,
        qty=5_000.0, entry_price=1.0, farb_position_id=fid,
    )
    # Also add a wallet snapshot so cash is populated
    await _insert_wallet_snapshot(session_factory, exchange_id, "USDC", balance=5_000.0)

    quotes = {"USDC": _quote("USDC", mark=1.0, spot=1.0)}
    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes=quotes)

    # COLLATERAL does NOT contribute to spot_value or perp_unrealized
    assert snap.spot_value == pytest.approx(0.0)
    assert snap.perp_unrealized == pytest.approx(0.0)
    # Cash = 5000 (from wallet snapshot)
    assert snap.cash == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# Test 11a: save_snapshot writes a row
# ---------------------------------------------------------------------------


async def test_save_snapshot_writes_row(session_factory, strategy_id):
    ledger = Ledger(session_factory)
    snap = EquitySnapshot(
        strategy_id=strategy_id,
        ts_ms=_NOW_MS,
        total_equity=12_345.67,
        cash=10_000.0,
        spot_value=2_000.0,
        perp_unrealized=345.67,
        perp_realized_cum=0.0,
        funding_cum=0.0,
        fees_cum=0.0,
    )
    await ledger.save_snapshot(snap)

    # Verify row exists in DB
    from sqlalchemy import select as sa_select
    async with session_factory() as session:
        result = await session.execute(
            sa_select(EquitySnapshotRow).where(
                EquitySnapshotRow.strategy_id == strategy_id,
                EquitySnapshotRow.ts_ms == _NOW_MS,
            )
        )
        row = result.scalar_one_or_none()

    assert row is not None
    assert row.total_equity == pytest.approx(12_345.67)
    assert row.cash == pytest.approx(10_000.0)
    assert row.spot_value == pytest.approx(2_000.0)
    assert row.perp_unrealized == pytest.approx(345.67)


# ---------------------------------------------------------------------------
# Test 11b: compute_and_save writes and returns snapshot
# ---------------------------------------------------------------------------


async def test_compute_and_save_writes_and_returns(session_factory, strategy_id, exchange_id):
    await _insert_wallet_snapshot(session_factory, exchange_id, "USDC", balance=3_000.0)

    ledger = Ledger(session_factory)
    snap = await ledger.compute_and_save(strategy_id=strategy_id, quotes={})

    # Returns an EquitySnapshot
    assert isinstance(snap, EquitySnapshot)
    assert snap.cash == pytest.approx(3_000.0)

    # Verify row is in DB
    from sqlalchemy import select as sa_select
    async with session_factory() as session:
        result = await session.execute(
            sa_select(EquitySnapshotRow).where(
                EquitySnapshotRow.strategy_id == strategy_id,
            )
        )
        rows = result.scalars().all()

    assert len(rows) == 1
    assert rows[0].cash == pytest.approx(3_000.0)


# ---------------------------------------------------------------------------
# Extra: spot position uses mark price when spot is None
# ---------------------------------------------------------------------------


async def test_spot_position_falls_back_to_mark_when_no_spot(session_factory, strategy_id, exchange_id):
    fid = await _insert_farb_position(session_factory, strategy_id, "ETH")
    await _insert_position(
        session_factory, exchange_id, "ETH", Instrument.SPOT, Side.LONG,
        qty=2.0, entry_price=3_000.0, farb_position_id=fid,
    )

    # Quote has no spot price (spot=None)
    quotes = {"ETH": _quote("ETH", mark=3_200.0, spot=None)}
    ledger = Ledger(session_factory)
    snap = await ledger.compute_equity(strategy_id=strategy_id, quotes=quotes)

    # Falls back to mark=3200
    assert snap.spot_value == pytest.approx(2 * 3_200.0)


# ---------------------------------------------------------------------------
# Extra: multiple strategies — only selected strategy counted
# ---------------------------------------------------------------------------


async def test_strategy_isolation(session_factory, exchange_id):
    from frab.db.models import Strategy as StrategyRow

    # Create two strategies
    async with session_scope(session_factory) as s:
        s1 = StrategyRow(name="s1", version="v1", params_json={})
        s2 = StrategyRow(name="s2", version="v1", params_json={})
        s.add(s1)
        s.add(s2)
        await s.flush()
        sid1, sid2 = s1.id, s2.id

    fid1 = await _insert_farb_position(session_factory, sid1, "BTC")
    fid2 = await _insert_farb_position(session_factory, sid2, "ETH")

    # Wallet snapshot for exchange — both strategies share the same exchange
    await _insert_wallet_snapshot(session_factory, exchange_id, "USDC", balance=10_000.0)

    # Spot positions — one per strategy
    await _insert_position(
        session_factory, exchange_id, "BTC", Instrument.SPOT, Side.LONG,
        qty=1.0, entry_price=50_000.0, farb_position_id=fid1,
    )
    await _insert_position(
        session_factory, exchange_id, "ETH", Instrument.SPOT, Side.LONG,
        qty=10.0, entry_price=3_000.0, farb_position_id=fid2,
    )

    quotes = {
        "BTC": _quote("BTC", mark=50_000.0, spot=50_000.0),
        "ETH": _quote("ETH", mark=3_000.0, spot=3_000.0),
    }
    ledger = Ledger(session_factory)

    snap1 = await ledger.compute_equity(strategy_id=sid1, quotes=quotes)
    snap2 = await ledger.compute_equity(strategy_id=sid2, quotes=quotes)

    # Strategy 1: only BTC position
    assert snap1.spot_value == pytest.approx(50_000.0)
    # Strategy 2: only ETH position
    assert snap2.spot_value == pytest.approx(30_000.0)


# ---------------------------------------------------------------------------
# Extra: WARN logged only once per coin (not per position)
# ---------------------------------------------------------------------------


async def test_missing_quote_warns_once_per_coin(session_factory, strategy_id, exchange_id, caplog):
    fid = await _insert_farb_position(session_factory, strategy_id, "BTC")
    # Two open positions for the same coin
    await _insert_position(
        session_factory, exchange_id, "BTC", Instrument.SPOT, Side.LONG,
        qty=0.5, entry_price=50_000.0, farb_position_id=fid,
    )
    await _insert_position(
        session_factory, exchange_id, "BTC", Instrument.PERP, Side.SHORT,
        qty=0.5, entry_price=50_000.0, farb_position_id=fid,
    )

    ledger = Ledger(session_factory)
    with caplog.at_level(logging.WARNING, logger="frab.ledger.ledger"):
        await ledger.compute_equity(strategy_id=strategy_id, quotes={})

    btc_warns = [r for r in caplog.records if "BTC" in r.message and r.levelno == logging.WARNING]
    assert len(btc_warns) == 1, f"Expected 1 WARN for BTC, got {len(btc_warns)}"
