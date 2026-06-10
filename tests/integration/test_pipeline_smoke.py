"""End-to-end pipeline smoke test.

Wires EngineLoop with a FULLY MOCKED Exchange (returns canned data) and
real FarbRepo + Ledger + TwoPhaseStrategy + in-memory SQLite DB.

Scenario:
  1. minute tick saves price rows
  2. on_hour_tick after seeding funding history → new FarbPosition(state=CHECK_MARGIN)
  3. advance_all_pending repeatedly → walks CHECK_MARGIN → PRE_BREAKEVEN
  4. inject low funding + force hours_held ≥ min_hold → CLOSING_SHORT
  5. advance_all_pending again → walks to CLOSED
  6. final assertions: FarbPosition CLOSED, all linked positions CLOSED,
     equity_snapshots has ≥ 1 row
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import (
    Exchange as ExchangeRow,
    FarbPosition as FarbPositionRow,
    FundingRate as FundingRateRow,
    Position as PositionRow,
    Price as PriceRow,
    Strategy as StrategyRow,
    EquitySnapshot as EquitySnapshotRow,
)
from frab.db.session import init_db, make_session_factory, session_scope
from frab.domain import FarbState, Instrument, PositionStatus, Side
from frab.domain.position import Position as DomainPosition
from frab.engine.loop import EngineLoop
from frab.exchanges.protocol import FundingTick, Quote, WalletKind
from frab.ledger.ledger import Ledger
from frab.repo.farb_repo import FarbRepo
from frab.settings import Settings
from frab.strategy.two_phase import TwoPhaseParams, TwoPhaseStrategy

_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC
_FIXED_DT = datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(eng.sync_engine, "connect", _enable_fks)
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return make_session_factory(engine)


@pytest_asyncio.fixture
async def seeded(session_factory):
    """Seed exchange + strategy rows; return (exchange_id, strategy_id)."""
    async with session_scope(session_factory) as s:
        exc = ExchangeRow(
            name="mock_exchange",
            funding_interval_h=1,
            spot_taker_bps=7.0,
            perp_taker_bps=3.5,
        )
        s.add(exc)
        strat = StrategyRow(
            name="two_phase_smoke",
            version="v2",
            params_json={"k": 1},
            status="running",
        )
        s.add(strat)
        await s.flush()
        eid = exc.id
        sid = strat.id
    return eid, sid


def _make_mock_exchange(session_factory, exchange_id: int):
    """Return an AsyncMock exchange that inserts real DB rows for open_position."""
    mock = AsyncMock()
    mock.name = "mock_exchange"

    mock.get_quote.return_value = Quote(
        coin="BTC", mark=50000.0, spot=50000.0, bid=49990.0, ask=50010.0, ts_ms=_NOW_MS
    )
    mock.get_funding_rate.return_value = FundingTick(
        coin="BTC", ts_ms=_NOW_MS, rate=0.0001, premium=0.0, annualized_pct=0.876
    )
    mock.get_wallet.return_value = 10000.0

    def _domain_from_req(req, pos_id: int) -> DomainPosition:
        return DomainPosition(
            id=pos_id,
            exchange_name="mock_exchange",
            coin=req.coin,
            instrument=req.instrument,
            side=req.side,
            qty=req.qty,
            entry_price=1.0 if req.instrument == Instrument.COLLATERAL else 50000.0,
            opened_at=_FIXED_DT,
            closed_at=None,
            status=PositionStatus.OPEN,
            farb_position_id=req.farb_position_id,
        )

    async def _open_pos(req):
        async with session_scope(session_factory) as s:
            row = PositionRow(
                exchange_id=exchange_id,
                coin=req.coin,
                instrument=req.instrument,
                side=req.side,
                qty=req.qty,
                entry_price=1.0 if req.instrument == Instrument.COLLATERAL else 50000.0,
                opened_at=_NOW_MS,
                closed_at=None,
                status=PositionStatus.OPEN,
                farb_position_id=None,
            )
            s.add(row)
            await s.flush()
            pid = row.id
        return _domain_from_req(req, pid)

    async def _close_pos(pos: DomainPosition) -> DomainPosition:
        """Update DB row to CLOSED in addition to returning the closed domain object."""
        import asyncio
        from sqlalchemy import update as sa_update
        async with session_scope(session_factory) as s:
            await s.execute(
                sa_update(PositionRow)
                .where(PositionRow.id == pos.id)
                .values(status=PositionStatus.CLOSED.value, closed_at=_NOW_MS)
            )
        return DomainPosition(
            id=pos.id,
            exchange_name=pos.exchange_name,
            coin=pos.coin,
            instrument=pos.instrument,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            opened_at=pos.opened_at,
            closed_at=_FIXED_DT,
            status=PositionStatus.CLOSED,
            farb_position_id=pos.farb_position_id,
        )

    mock.open_position.side_effect = _open_pos
    mock.close_position.side_effect = _close_pos
    mock.transfer.return_value = None
    mock.round_qty_to_nearest = AsyncMock(side_effect=lambda coin, qty: qty)

    return mock


# ---------------------------------------------------------------------------
# The smoke test
# ---------------------------------------------------------------------------

async def test_pipeline_smoke(session_factory, seeded):
    """Full pipeline: EngineLoop minute/hour tick → strategy → FarbPosition CLOSED."""
    exchange_id, strategy_id = seeded

    exchange = _make_mock_exchange(session_factory, exchange_id)
    farb_repo = FarbRepo(session_factory)
    ledger = Ledger(session_factory)

    params = TwoPhaseParams(
        coins=["BTC"],
        entry_threshold_apr=0.10,
        phase2_exit_threshold=-0.10,
        base_min_hold_hours=24,
        cap_min_hold_hours=720,
        safety_mult=5.0,
        signal_window_hours=3,
        concurrency_cap=3,
        position_size_usdc=1000.0,
        margin_buffer_factor=3.0,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    settings = MagicMock(spec=Settings)
    from frab.constants import CoinMarginSpec
    settings.get_coin_spec.return_value = CoinMarginSpec(leverage=5, maint_ratio=0.025)
    strategy = TwoPhaseStrategy(
        strategy_id=strategy_id,
        exchange=exchange,
        farb_repo=farb_repo,
        session_factory=session_factory,
        params=params,
        settings=settings,
    )

    loop = EngineLoop(
        strategy=strategy,
        exchange=exchange,
        ledger=ledger,
        session_factory=session_factory,
        coins=["BTC"],
        minute_interval_s=60.0,
    )

    # ── Tick 1: minute tick saves prices ──────────────────────────────────────
    await loop._minute_tick(_NOW_MS)

    async with session_scope(session_factory) as s:
        prices = (await s.execute(select(PriceRow))).scalars().all()
    assert len(prices) >= 1, "minute tick should save at least one price row"
    assert prices[0].coin == "BTC"

    # ── Seed 3 funding rate rows (signal_window_hours=3) ──────────────────────
    # APR = 0.0001 * 8760 = 0.876 > entry_threshold=0.10 → ENTER
    async with session_scope(session_factory) as s:
        for i in range(3):
            s.add(FundingRateRow(
                exchange_id=exchange_id,
                coin="BTC",
                ts_ms=_NOW_MS - (2 - i) * 3_600_000,
                rate=0.0001,
                premium=0.0,
                annualized_pct=0.876,
            ))

    # ── Hour tick: entry decision → new FarbPosition(CHECK_MARGIN) ───────────
    await strategy.on_hour_tick(now_ms=_NOW_MS)

    async with session_scope(session_factory) as s:
        fps = (await s.execute(
            select(FarbPositionRow).where(FarbPositionRow.strategy_id == strategy_id)
        )).scalars().all()
    assert len(fps) == 1
    assert fps[0].state == FarbState.CHECK_MARGIN.value
    assert fps[0].coin == "BTC"
    fp_id = fps[0].id

    # ── Advance CHECK_MARGIN → PRE_BREAKEVEN (single burst call) ─────────────
    fp = await farb_repo.get(fp_id)
    await strategy._advance_one(fp)

    fp = await farb_repo.get(fp_id)
    assert fp is not None
    assert fp.state == FarbState.PRE_BREAKEVEN, f"Expected PRE_BREAKEVEN, got {fp.state}"
    assert fp.spot_position_id is not None
    assert fp.perp_position_id is not None
    assert fp.margin_position_id is not None

    # ── Inject low signal + override hours_held so exit triggers ─────────────
    # Seed negative funding rates (below phase2_exit_threshold=-0.10)
    exit_base_ms = _NOW_MS + 10 * 3_600_000
    async with session_scope(session_factory) as s:
        for i in range(3):
            s.add(FundingRateRow(
                exchange_id=exchange_id,
                coin="BTC",
                ts_ms=exit_base_ms - (2 - i) * 3_600_000,
                rate=-0.00002,  # -0.175 APR < -0.10 threshold
                premium=0.0,
                annualized_pct=-0.175,
            ))

    # Force opened_at_ms far in the past so hours_held >> min_hold
    # gross_funding < total_fees → PRE_BREAKEVEN (phase 1 logic)
    # consec_negative_hours=10 >= neg_stop_patience=6; signal -0.175 < -0.15 threshold
    # → CLOSE_PRE_BE_NEGSTOP fires, bypassing min_hold
    fp = await farb_repo.get(fp_id)
    new_sd = {
        **fp.state_data,
        "opened_at_ms": _NOW_MS - 200 * 3_600_000,
        "position_min_hold_hours": 24,
        "gross_funding_so_far": 0.5,   # below total_fees → PRE_BREAKEVEN
        "total_fees_paid": 4.2,
        "consec_negative_hours": 10,   # >= neg_stop_patience=6
    }
    await farb_repo.update_state_data(fp_id, new_sd)

    await strategy._evaluate_exits(now_ms=exit_base_ms)

    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.CLOSING_SHORT, f"Expected CLOSING_SHORT, got {fp.state}"

    # ── Advance CLOSING_SHORT → CLOSED (single burst call) ───────────────────
    fp = await farb_repo.get(fp_id)
    await strategy._advance_one(fp)

    fp = await farb_repo.get(fp_id)
    assert fp is not None
    assert fp.state == FarbState.CLOSED, f"Expected CLOSED, got {fp.state}"

    # ── Final assertions ──────────────────────────────────────────────────────

    # All linked positions should be CLOSED in DB
    async with session_scope(session_factory) as s:
        for pid in [fp.spot_position_id, fp.perp_position_id]:
            if pid is not None:
                pos_row = await s.get(PositionRow, pid)
                assert pos_row is not None
                assert pos_row.status == PositionStatus.CLOSED.value, (
                    f"Position {pid} should be CLOSED but is {pos_row.status}"
                )

    # equity_snapshots has at least one row (written by ledger during minute tick)
    async with session_scope(session_factory) as s:
        eq_rows = (await s.execute(
            select(EquitySnapshotRow).where(EquitySnapshotRow.strategy_id == strategy_id)
        )).scalars().all()
    assert len(eq_rows) >= 1, "Expected at least one equity_snapshots row"
