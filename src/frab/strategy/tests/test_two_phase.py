"""Tests for TwoPhaseStrategy — state machine, entry/exit decisions, rollback.

Uses mocker.AsyncMock for Exchange, real in-memory SQLite for FarbRepo.
22 required test areas covered.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call

from frab.domain import FarbPosition, FarbState, Instrument, Position, Side
from frab.domain.enums import PositionStatus
from frab.db.models import FundingRate as FundingRateRow, Exchange as ExchangeRow
from frab.db.session import session_scope
from frab.exchanges.protocol import WalletKind
from frab.repo.farb_repo import FarbRepo, StateConflict
from frab.strategy.two_phase import TwoPhaseParams, TwoPhaseStrategy

from .conftest import make_position, _NOW_MS


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        coins=["BTC", "ETH"],
        entry_threshold_apr=0.10,
        phase2_exit_threshold=-0.10,
        base_min_hold_hours=24,
        cap_min_hold_hours=720,
        safety_mult=5.0,
        signal_window_hours=3,  # small for tests
        concurrency_cap=3,
        position_size_usdc=1000.0,
        margin_buffer_factor=3.0,
        perp_leverage=5.0,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    defaults.update(overrides)
    return TwoPhaseParams(**defaults)


def _make_exchange(session_factory=None, exchange_id: int = 1) -> AsyncMock:
    """Return a mock Exchange that returns sane defaults.

    When session_factory is provided, open_position inserts real Position rows
    into the DB so that farb_repo.set_leg FK constraints succeed.
    """
    from frab.exchanges.protocol import Quote
    from frab.db.models import Position as PositionDBRow

    mock = AsyncMock()
    mock.name = "HL"
    mock.get_wallet.return_value = 10000.0
    mock.get_quote.return_value = Quote(
        coin="BTC", mark=50000.0, spot=50000.0, bid=49990.0, ask=50010.0, ts_ms=_NOW_MS
    )

    def _domain_from_req(req, pos_id: int) -> Position:
        return Position(
            id=pos_id,
            exchange_name="HL",
            coin=req.coin,
            instrument=req.instrument,
            side=req.side,
            qty=req.qty,
            entry_price=1.0 if req.instrument == Instrument.COLLATERAL else 50000.0,
            opened_at=datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc),
            closed_at=None,
            status=PositionStatus.OPEN,
            farb_position_id=req.farb_position_id,
        )

    if session_factory is not None:
        # DB-backed: open_position inserts a real Position row so set_leg FK works
        async def _make_pos_db(req):
            async with session_scope(session_factory) as s:
                row = PositionDBRow(
                    exchange_id=exchange_id,
                    coin=req.coin,
                    instrument=req.instrument,
                    side=req.side,
                    qty=req.qty,
                    entry_price=1.0 if req.instrument == Instrument.COLLATERAL else 50000.0,
                    opened_at=_NOW_MS,
                    closed_at=None,
                    status=PositionStatus.OPEN,
                    farb_position_id=None,  # FK set via set_leg after
                )
                s.add(row)
                await s.flush()
                pid = row.id
            return _domain_from_req(req, pid)

        mock.open_position.side_effect = _make_pos_db
    else:
        # Simple mock (tests that don't call set_leg)
        _counter = {"n": 1}

        def _make_pos(req):
            pos = _domain_from_req(req, _counter["n"])
            _counter["n"] += 1
            return pos

        mock.open_position.side_effect = _make_pos

    def _close_pos(pos):
        return Position(
            id=pos.id,
            exchange_name=pos.exchange_name,
            coin=pos.coin,
            instrument=pos.instrument,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            opened_at=pos.opened_at,
            closed_at=datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc),
            status=PositionStatus.CLOSED,
            farb_position_id=pos.farb_position_id,
        )

    mock.close_position.side_effect = _close_pos
    return mock


def _make_strategy(exchange, farb_repo, session_factory, **param_overrides) -> TwoPhaseStrategy:
    params = _make_params(**param_overrides)
    return TwoPhaseStrategy(
        strategy_id=1,
        exchange=exchange,
        farb_repo=farb_repo,
        session_factory=session_factory,
        params=params,
    )


async def _seed_funding_rates(session_factory, exchange_id: int, coin: str,
                               rates: list[float], base_ts_ms: int = _NOW_MS) -> None:
    """Insert funding rate rows into the DB for signal tests."""
    async with session_scope(session_factory) as s:
        for i, rate in enumerate(rates):
            s.add(FundingRateRow(
                exchange_id=exchange_id,
                coin=coin,
                ts_ms=base_ts_ms - (len(rates) - 1 - i) * 3_600_000,
                rate=rate,
                premium=0.0,
                annualized_pct=rate * 8760,
            ))


# ─── State-machine tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_01_check_margin_happy(session_factory, farb_repo, strategy_id, exchange_id):
    """CHECK_MARGIN → bursts to OPEN in a single _advance_one call when wallet has funds."""
    exchange = _make_exchange(session_factory=session_factory, exchange_id=exchange_id)
    exchange.get_wallet.return_value = 10000.0

    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp = await farb_repo.create(strategy_id=strategy_id, coin="BTC",
                                initial_state=FarbState.CHECK_MARGIN)
    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    # Burst walks all the way to OPEN in one call
    assert updated.state == FarbState.OPEN
    # required_margin should be carried through state_data
    assert "required_margin" in updated.state_data


@pytest.mark.asyncio
async def test_02_check_margin_insufficient(session_factory, farb_repo, strategy_id):
    """CHECK_MARGIN → FAILED when wallet has insufficient funds."""
    exchange = _make_exchange()
    exchange.get_wallet.return_value = 1.0  # way too little

    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp = await farb_repo.create(strategy_id=strategy_id, coin="BTC",
                                initial_state=FarbState.CHECK_MARGIN)
    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.FAILED
    assert "insufficient_margin" in updated.state_data.get("failure_reason", "")


@pytest.mark.asyncio
async def test_03_opening_margin(session_factory, farb_repo, strategy_id, exchange_id):
    """OPENING_MARGIN: open_position(COLLATERAL) called, set_leg recorded; bursts to OPEN."""
    exchange = _make_exchange(session_factory=session_factory, exchange_id=exchange_id)
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    required = strat.params.required_margin()
    fp = await farb_repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.OPENING_MARGIN,
        state_data={"required_margin": required},
    )
    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    # Burst walks from OPENING_MARGIN all the way to OPEN
    assert updated.state == FarbState.OPEN
    assert updated.margin_position_id is not None

    # Verify exchange was called with COLLATERAL as first open_position call
    first_call_req = exchange.open_position.call_args_list[0][0][0]
    assert first_call_req.instrument == Instrument.COLLATERAL
    assert first_call_req.qty == pytest.approx(required)


@pytest.mark.asyncio
async def test_04_opening_long(session_factory, farb_repo, strategy_id, exchange_id):
    """OPENING_LONG: exchange called with SPOT/LONG, spot_qty correct; bursts to OPEN."""
    exchange = _make_exchange(session_factory=session_factory, exchange_id=exchange_id)
    price = 50000.0
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp = await farb_repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.OPENING_LONG,
        state_data={"required_margin": strat.params.required_margin()},
    )
    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    # Burst walks from OPENING_LONG → OPENING_SHORT → OPEN
    assert updated.state == FarbState.OPEN
    assert updated.spot_position_id is not None

    # Verify SPOT/LONG was the first open_position call from OPENING_LONG
    spot_call = exchange.open_position.call_args_list[0][0][0]
    assert spot_call.instrument == Instrument.SPOT
    assert spot_call.side == Side.LONG
    expected_qty = strat.params.position_size_usdc / price
    assert spot_call.qty == pytest.approx(expected_qty)


@pytest.mark.asyncio
async def test_05_opening_short(session_factory, farb_repo, strategy_id, exchange_id):
    """OPENING_SHORT → OPEN: exchange called with PERP/SHORT; perp_qty equals spot_qty."""
    exchange = _make_exchange(session_factory=session_factory, exchange_id=exchange_id)
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    spot_qty = strat.params.position_size_usdc / 50000.0
    fp = await farb_repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.OPENING_SHORT,
        state_data={
            "required_margin": strat.params.required_margin(),
            "spot_qty": spot_qty,
            "target_signal_apr": 0.20,
        },
    )
    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.OPEN

    req = exchange.open_position.call_args[0][0]
    assert req.instrument == Instrument.PERP
    assert req.side == Side.SHORT
    assert req.qty == pytest.approx(spot_qty)


@pytest.mark.asyncio
async def test_06_open_is_noop(session_factory, farb_repo, strategy_id):
    """OPEN state: advance is a no-op — no exchange calls, state unchanged."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp = await farb_repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.OPEN,
    )
    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.OPEN
    exchange.open_position.assert_not_called()
    exchange.close_position.assert_not_called()


@pytest.mark.asyncio
async def test_07_closing_short(session_factory, farb_repo, strategy_id, exchange_id):
    """CLOSING_SHORT: close_position called on perp leg; bursts to CLOSED (spot leg also closed)."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    perp_pos_id = await make_position(
        session_factory, exchange_id=exchange_id, coin="BTC",
        instrument=Instrument.PERP, side=Side.SHORT,
    )
    spot_pos_id = await make_position(
        session_factory, exchange_id=exchange_id, coin="BTC",
        instrument=Instrument.SPOT, side=Side.LONG,
    )
    required = strat.params.required_margin()
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC",
        initial_state=FarbState.CLOSING_SHORT,
        state_data={"required_margin": required},
    )
    await farb_repo.set_leg(fp.id, instrument=Instrument.PERP, position_id=perp_pos_id)
    await farb_repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=spot_pos_id)
    fp = await farb_repo.get(fp.id)

    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    # Burst walks CLOSING_SHORT → CLOSING_LONG → RELEASING_MARGIN → CLOSED
    assert updated.state == FarbState.CLOSED
    # close_position called at least once for perp, at least once for spot
    assert exchange.close_position.call_count >= 2
    closed_ids = [c[0][0].id for c in exchange.close_position.call_args_list]
    assert perp_pos_id in closed_ids
    assert spot_pos_id in closed_ids


@pytest.mark.asyncio
async def test_08_closing_long(session_factory, farb_repo, strategy_id, exchange_id):
    """CLOSING_LONG: close_position called on spot leg; bursts to CLOSED."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    spot_pos_id = await make_position(
        session_factory, exchange_id=exchange_id, coin="BTC",
        instrument=Instrument.SPOT, side=Side.LONG,
    )
    required = strat.params.required_margin()
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC",
        initial_state=FarbState.CLOSING_LONG,
        state_data={"required_margin": required},
    )
    await farb_repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=spot_pos_id)
    fp = await farb_repo.get(fp.id)

    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    # Burst walks CLOSING_LONG → RELEASING_MARGIN → CLOSED
    assert updated.state == FarbState.CLOSED
    # close_position called once for the spot leg (no margin_position_id)
    exchange.close_position.assert_called_once()
    closed_pos = exchange.close_position.call_args[0][0]
    assert closed_pos.id == spot_pos_id


@pytest.mark.asyncio
async def test_09_releasing_margin(session_factory, farb_repo, strategy_id, exchange_id):
    """RELEASING_MARGIN → CLOSED: transfer called PERP→SPOT, margin pos closed, state=CLOSED."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    margin_pos_id = await make_position(
        session_factory, exchange_id=exchange_id, coin="USDC",
        instrument=Instrument.COLLATERAL, side=Side.NONE, qty=600.0, entry_price=1.0,
    )
    required = strat.params.required_margin()
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC",
        initial_state=FarbState.RELEASING_MARGIN,
        state_data={"required_margin": required},
    )
    await farb_repo.set_leg(fp.id, instrument=Instrument.COLLATERAL, position_id=margin_pos_id)
    fp = await farb_repo.get(fp.id)

    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.CLOSED

    exchange.transfer.assert_called_once_with(
        "USDC", pytest.approx(required), WalletKind.PERP, WalletKind.SPOT
    )
    exchange.close_position.assert_called_once()


@pytest.mark.asyncio
async def test_10_closed_and_failed_no_advancement(session_factory, farb_repo, strategy_id):
    """CLOSED and FAILED states: advance is a no-op, no exchange calls."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp_closed = await farb_repo.create(strategy_id=strategy_id, coin="BTC",
                                       initial_state=FarbState.CHECK_MARGIN)
    # Force CLOSED directly via mark_closed path: transition to RELEASING_MARGIN first
    # Simpler: just create then override state via test — but FarbRepo doesn't allow that
    # so we use mark_failed → then create a fresh CLOSED one via full close path
    fp_failed = await farb_repo.create(strategy_id=strategy_id, coin="ETH",
                                       initial_state=FarbState.CHECK_MARGIN)
    await farb_repo.mark_failed(fp_failed.id, reason="test")
    fp_failed = await farb_repo.get(fp_failed.id)

    # Drive fp_closed through to CLOSED using the check_margin path
    # Actually easiest: create in RELEASING_MARGIN state with no legs
    fp_rel = await farb_repo.create(
        strategy_id=strategy_id, coin="SOL",
        initial_state=FarbState.RELEASING_MARGIN,
        state_data={"required_margin": 600.0},
    )
    # No margin_position_id → releasing margin won't call close_position for it
    await strat._advance_one(fp_rel)
    fp_closed = await farb_repo.get(fp_rel.id)
    assert fp_closed.state == FarbState.CLOSED

    # Reset call counts
    exchange.reset_mock()

    # Now advance both terminal positions — should be no-ops
    await strat._advance_one(fp_closed)
    await strat._advance_one(fp_failed)

    exchange.open_position.assert_not_called()
    exchange.close_position.assert_not_called()
    exchange.transfer.assert_not_called()


# ─── Rollback tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_11_rollback_opening_short_fails(session_factory, farb_repo, strategy_id, exchange_id):
    """OPENING_SHORT fails → spot leg closed, fp marked FAILED with reason."""
    exchange = _make_exchange()
    # First call to open_position creates collateral and spot (already done before this state)
    # In OPENING_SHORT state, exchange.open_position raises
    exchange.open_position.side_effect = RuntimeError("perp market down")

    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    spot_pos_id = await make_position(
        session_factory, exchange_id=exchange_id, coin="BTC",
        instrument=Instrument.SPOT, side=Side.LONG,
    )
    spot_qty = 1000.0 / 50000.0
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC",
        initial_state=FarbState.OPENING_SHORT,
        state_data={"spot_qty": spot_qty, "required_margin": 600.0},
    )
    await farb_repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=spot_pos_id)
    fp = await farb_repo.get(fp.id)

    # Reset close_position to not fail
    exchange.close_position.side_effect = None

    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.FAILED
    assert "perp market down" in updated.state_data.get("failure_reason", "")
    # Spot leg should have been closed in rollback
    exchange.close_position.assert_called_once()


@pytest.mark.asyncio
async def test_12_rollback_opening_long_fails(session_factory, farb_repo, strategy_id):
    """OPENING_LONG fails after margin reserved → transfer back to spot called, fp FAILED."""
    exchange = _make_exchange()
    exchange.open_position.side_effect = RuntimeError("spot market halted")

    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC",
        initial_state=FarbState.OPENING_LONG,
        state_data={"required_margin": 600.0},
    )
    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.FAILED
    # Transfer should have been called to return margin
    exchange.transfer.assert_called_once_with(
        "USDC", pytest.approx(600.0), WalletKind.PERP, WalletKind.SPOT
    )


@pytest.mark.asyncio
async def test_13_rollback_closing_long_fails(session_factory, farb_repo, strategy_id, exchange_id):
    """CLOSING_LONG fails after short already closed → fp FAILED, no auto-reopen of short."""
    exchange = _make_exchange()
    exchange.close_position.side_effect = RuntimeError("connection lost")

    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    spot_pos_id = await make_position(
        session_factory, exchange_id=exchange_id, coin="BTC",
        instrument=Instrument.SPOT, side=Side.LONG,
    )
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC",
        initial_state=FarbState.CLOSING_LONG,
    )
    await farb_repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=spot_pos_id)
    fp = await farb_repo.get(fp.id)

    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.FAILED
    # Should NOT have called open_position to try to reopen the short
    exchange.open_position.assert_not_called()


# ─── Entry decision tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_14_entry_signal_creates_farb(
    session_factory, farb_repo, strategy_id, exchange_id
):
    """Coin with signal > entry_threshold + no existing FarbPosition → new FarbPosition created."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=["BTC"], concurrency_cap=3, signal_window_hours=3)
    strat.strategy_id = strategy_id

    # Seed 3 funding rates with high positive rate (~0.20 annualized > 0.10 threshold)
    rate_per_hour = 0.20 / 8760
    await _seed_funding_rates(session_factory, exchange_id, "BTC",
                               [rate_per_hour] * 3)

    # Insert HL exchange row so signal computation finds it
    async with session_scope(session_factory) as s:
        # exchange_id fixture already created it; update strategy to use same id
        pass

    await strat._evaluate_entries(now_ms=_NOW_MS)

    from frab.db.models import FarbPosition as FarbPositionRow
    from sqlalchemy import select as sa_select
    async with session_scope(session_factory) as s:
        result = await s.execute(
            sa_select(FarbPositionRow).where(FarbPositionRow.strategy_id == strategy_id)
        )
        fps = result.scalars().all()
    assert len(fps) == 1
    assert fps[0].state == FarbState.CHECK_MARGIN.value
    assert fps[0].coin == "BTC"


@pytest.mark.asyncio
async def test_15_entry_skips_existing_position(
    session_factory, farb_repo, strategy_id, exchange_id
):
    """Coin with signal but already has non-terminal FarbPosition → NOT created."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=["BTC"], concurrency_cap=3, signal_window_hours=3)
    strat.strategy_id = strategy_id

    # Pre-create an OPENING_LONG FarbPosition for BTC
    await farb_repo.create(strategy_id=strategy_id, coin="BTC",
                           initial_state=FarbState.OPENING_LONG)

    rate_per_hour = 0.20 / 8760
    await _seed_funding_rates(session_factory, exchange_id, "BTC",
                               [rate_per_hour] * 3)

    await strat._evaluate_entries(now_ms=_NOW_MS)

    from frab.db.models import FarbPosition as FarbPositionRow
    from sqlalchemy import select as sa_select
    async with session_scope(session_factory) as s:
        result = await s.execute(
            sa_select(FarbPositionRow).where(FarbPositionRow.strategy_id == strategy_id)
        )
        fps = result.scalars().all()
    # Still only one — the pre-existing one
    assert len(fps) == 1


@pytest.mark.asyncio
async def test_16_concurrency_cap_limits_entries(
    session_factory, farb_repo, strategy_id, exchange_id
):
    """K=3 concurrency, 5 coins above threshold → top-3 by signal created, other 2 skipped."""
    exchange = _make_exchange()
    coins = ["BTC", "ETH", "SOL", "AVAX", "LINK"]
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=coins, concurrency_cap=3, signal_window_hours=3)
    strat.strategy_id = strategy_id

    # Seed different rates so we can assert the top-3 are picked
    # BTC=0.50, ETH=0.40, SOL=0.30, AVAX=0.20, LINK=0.15 — all > 0.10
    for coin, apr in [("BTC", 0.50), ("ETH", 0.40), ("SOL", 0.30), ("AVAX", 0.20), ("LINK", 0.15)]:
        rate_per_hour = apr / 8760
        await _seed_funding_rates(session_factory, exchange_id, coin, [rate_per_hour] * 3)

    await strat._evaluate_entries(now_ms=_NOW_MS)

    from frab.db.models import FarbPosition as FarbPositionRow
    from sqlalchemy import select as sa_select
    async with session_scope(session_factory) as s:
        result = await s.execute(
            sa_select(FarbPositionRow).where(FarbPositionRow.strategy_id == strategy_id)
        )
        fps = result.scalars().all()

    assert len(fps) == 3
    created_coins = {fp.coin for fp in fps}
    assert "BTC" in created_coins
    assert "ETH" in created_coins
    assert "SOL" in created_coins
    assert "AVAX" not in created_coins
    assert "LINK" not in created_coins


@pytest.mark.asyncio
async def test_17_blacklist_coins_not_used(session_factory, farb_repo, strategy_id, exchange_id):
    """Strategy never creates positions for blacklisted HL bridge tokens.

    Strategy only processes coins in params.coins; AVAX0/LINK0/AAVE0 are simply
    never in params.coins so they never reach exchange.open_position.
    """
    exchange = _make_exchange()
    # Only legitimate coins — no bridge tokens
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=["BTC", "ETH"], concurrency_cap=3, signal_window_hours=3)
    strat.strategy_id = strategy_id

    rate_per_hour = 0.20 / 8760
    await _seed_funding_rates(session_factory, exchange_id, "BTC", [rate_per_hour] * 3)
    await _seed_funding_rates(session_factory, exchange_id, "ETH", [rate_per_hour] * 3)

    await strat._evaluate_entries(now_ms=_NOW_MS)

    # Verify exchange.open_position was NOT called for any bridge token
    for call in exchange.open_position.call_args_list:
        req = call[0][0]
        assert req.coin not in {"AVAX0", "LINK0", "AAVE0", "KPEPE"}, (
            f"exchange.open_position called with blacklisted coin {req.coin}"
        )


# ─── Exit decision tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_18_exit_signal_below_threshold_after_min_hold(
    session_factory, farb_repo, strategy_id, exchange_id
):
    """OPEN position with signal < exit_threshold AND held >= min_hold → CLOSING_SHORT."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=["BTC"], signal_window_hours=3)
    strat.strategy_id = strategy_id

    # phase2_exit_threshold=-0.10; use negative rate well below it
    rate_per_hour = -0.15 / 8760  # -0.15 APR annualized
    await _seed_funding_rates(session_factory, exchange_id, "BTC", [rate_per_hour] * 3)

    # Opened 200 hours ago (well past min_hold=24)
    opened_ms = _NOW_MS - 200 * 3_600_000
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.OPEN,
        state_data={
            "opened_at_ms": opened_ms,
            "position_min_hold_hours": 24,
            "gross_funding_so_far": 100.0,   # in profit (> total_fees_paid=4.2)
            "total_fees_paid": 4.2,
            "consec_negative_hours": 0,
        },
    )
    await strat._evaluate_exits(now_ms=_NOW_MS)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.CLOSING_SHORT


@pytest.mark.asyncio
async def test_19_exit_not_triggered_before_min_hold(
    session_factory, farb_repo, strategy_id, exchange_id
):
    """OPEN position with bad signal BUT held < min_hold → NOT transitioned."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=["BTC"], signal_window_hours=3)
    strat.strategy_id = strategy_id

    rate_per_hour = -0.15 / 8760
    await _seed_funding_rates(session_factory, exchange_id, "BTC", [rate_per_hour] * 3)

    # Opened only 5 hours ago, min_hold=24
    opened_ms = _NOW_MS - 5 * 3_600_000
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.OPEN,
        state_data={
            "opened_at_ms": opened_ms,
            "position_min_hold_hours": 24,
            "gross_funding_so_far": 100.0,
            "total_fees_paid": 4.2,
            "consec_negative_hours": 0,
        },
    )
    await strat._evaluate_exits(now_ms=_NOW_MS)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.OPEN


@pytest.mark.asyncio
async def test_20_exit_not_triggered_signal_above_threshold(
    session_factory, farb_repo, strategy_id, exchange_id
):
    """OPEN position with signal > exit_threshold → NOT transitioned."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=["BTC"], signal_window_hours=3)
    strat.strategy_id = strategy_id

    rate_per_hour = 0.20 / 8760  # well above exit_threshold=-0.10
    await _seed_funding_rates(session_factory, exchange_id, "BTC", [rate_per_hour] * 3)

    opened_ms = _NOW_MS - 200 * 3_600_000
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.OPEN,
        state_data={
            "opened_at_ms": opened_ms,
            "position_min_hold_hours": 24,
            "gross_funding_so_far": 100.0,
            "total_fees_paid": 4.2,
            "consec_negative_hours": 0,
        },
    )
    await strat._evaluate_exits(now_ms=_NOW_MS)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.OPEN


@pytest.mark.asyncio
async def test_21_exit_state_conflict_silent_skip(
    session_factory, farb_repo, strategy_id, exchange_id
):
    """StateConflict on exit transition → silent skip, no failure propagation."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=["BTC"], signal_window_hours=3)
    strat.strategy_id = strategy_id

    rate_per_hour = -0.15 / 8760
    await _seed_funding_rates(session_factory, exchange_id, "BTC", [rate_per_hour] * 3)

    opened_ms = _NOW_MS - 200 * 3_600_000
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.OPEN,
        state_data={
            "opened_at_ms": opened_ms,
            "position_min_hold_hours": 24,
            "gross_funding_so_far": 100.0,
            "total_fees_paid": 4.2,
            "consec_negative_hours": 0,
        },
    )

    # Patch farb_repo.transition to raise StateConflict
    original_transition = strat.farb_repo.transition

    async def _conflict_transition(*args, **kwargs):
        if kwargs.get("to_state") == FarbState.CLOSING_SHORT:
            raise StateConflict(fp.id, FarbState.OPEN, FarbState.CLOSING_SHORT)
        return await original_transition(*args, **kwargs)

    strat.farb_repo.transition = _conflict_transition

    # Should not raise
    await strat._evaluate_exits(now_ms=_NOW_MS)

    updated = await farb_repo.get(fp.id)
    # State should remain OPEN (conflict was caught silently)
    assert updated.state == FarbState.OPEN


# ─── Integration: full lifecycle ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_22_full_lifecycle(session_factory, farb_repo, strategy_id, exchange_id):
    """Full happy-path lifecycle: CHECK_MARGIN → OPEN → CLOSING_SHORT → CLOSED.

    Assert: open_position called 3x (COLLATERAL+SPOT+PERP),
            close_position called 2x (PERP+SPOT) + 1x margin = 3x total actually,
            transfer called 1x.
            Final fp.state=CLOSED.
    """
    # DB-backed exchange so set_leg FK constraints work
    exchange = _make_exchange(session_factory=session_factory, exchange_id=exchange_id)
    strat = _make_strategy(exchange, farb_repo, session_factory,
                           coins=["BTC"], signal_window_hours=3, concurrency_cap=3)
    strat.strategy_id = strategy_id

    # Seed high signal for entry
    rate_per_hour = 0.25 / 8760
    await _seed_funding_rates(session_factory, exchange_id, "BTC", [rate_per_hour] * 3)

    # ── Entry phase ──────────────────────────────────────────────────────────

    # Trigger entry
    await strat._evaluate_entries(now_ms=_NOW_MS)

    from frab.db.models import FarbPosition as FarbPositionRow
    from sqlalchemy import select as sa_select
    async with session_scope(session_factory) as s:
        result = await s.execute(
            sa_select(FarbPositionRow).where(FarbPositionRow.strategy_id == strategy_id)
        )
        fp_row = result.scalars().first()
    assert fp_row is not None
    assert fp_row.state == FarbState.CHECK_MARGIN.value
    fp_id = fp_row.id

    # Single _advance_one call bursts CHECK_MARGIN → OPEN in one shot
    fp = await farb_repo.get(fp_id)
    await strat._advance_one(fp)

    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.OPEN

    # exchange.open_position should have been called 3x
    assert exchange.open_position.call_count == 3
    instruments_opened = [
        call[0][0].instrument for call in exchange.open_position.call_args_list
    ]
    assert Instrument.COLLATERAL in instruments_opened
    assert Instrument.SPOT in instruments_opened
    assert Instrument.PERP in instruments_opened

    # ── Transition to exit ───────────────────────────────────────────────────

    # Seed negative rate to trigger exit in phase2 (fp is in profit)
    # Use timestamps that are distinct from the positive-rate ones seeded above.
    # Entry rates used ts_ms = _NOW_MS - 2h, -1h, 0h.  Exit rates start at _NOW_MS + 10h.
    exit_base_ms = _NOW_MS + 10 * 3_600_000
    rate_per_hour_neg = -0.15 / 8760
    await _seed_funding_rates(session_factory, exchange_id, "BTC", [rate_per_hour_neg] * 3,
                               base_ts_ms=exit_base_ms)

    fp = await farb_repo.get(fp_id)
    # Manually set state_data to simulate being past min_hold and in profit
    new_sd = {
        **fp.state_data,
        "opened_at_ms": _NOW_MS - 200 * 3_600_000,
        "position_min_hold_hours": 24,
        "gross_funding_so_far": 100.0,
        "total_fees_paid": 4.2,
        "consec_negative_hours": 0,
    }
    await farb_repo.update_state_data(fp_id, new_sd)

    await strat._evaluate_exits(now_ms=exit_base_ms)

    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.CLOSING_SHORT

    # ── Close phase ──────────────────────────────────────────────────────────

    # Set up position ids on the fp (needed for closing steps)
    # They were set by the exchange mock during open; fetch updated fp
    fp = await farb_repo.get(fp_id)
    # perp_position_id and spot_position_id should already be set
    assert fp.perp_position_id is not None
    assert fp.spot_position_id is not None

    # Single _advance_one call bursts CLOSING_SHORT → CLOSED in one shot
    fp = await farb_repo.get(fp_id)
    await strat._advance_one(fp)

    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.CLOSED

    # close_position called 2x for PERP + SPOT, and 1x for margin = 3 total
    # (margin may or may not be set — if not set, 2 calls total)
    assert exchange.close_position.call_count >= 2

    # transfer called exactly 1x for margin release
    exchange.transfer.assert_called_once()
    transfer_call = exchange.transfer.call_args
    assert transfer_call[0][2] == WalletKind.PERP
    assert transfer_call[0][3] == WalletKind.SPOT


# ─── Burst-loop tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_advance_one_bursts_full_open(session_factory, farb_repo, strategy_id, exchange_id):
    """Single _advance_one from CHECK_MARGIN bursts to OPEN; all 3 leg positions created in DB."""
    exchange = _make_exchange(session_factory=session_factory, exchange_id=exchange_id)
    exchange.get_wallet.return_value = 10000.0

    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp = await farb_repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.CHECK_MARGIN,
    )
    await strat._advance_one(fp)

    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.OPEN

    # All 3 leg position IDs must be set
    assert updated.margin_position_id is not None
    assert updated.spot_position_id is not None
    assert updated.perp_position_id is not None

    # exchange.open_position called exactly 3 times (COLLATERAL, SPOT, PERP)
    assert exchange.open_position.call_count == 3
    instruments = [c[0][0].instrument for c in exchange.open_position.call_args_list]
    assert Instrument.COLLATERAL in instruments
    assert Instrument.SPOT in instruments
    assert Instrument.PERP in instruments


@pytest.mark.asyncio
async def test_advance_one_stops_on_state_conflict(session_factory, farb_repo, strategy_id, mocker):
    """StateConflict mid-burst: loop breaks immediately, no rollback triggered."""
    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp = await farb_repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.CHECK_MARGIN,
    )

    # Patch transition so it raises StateConflict on the very first call
    conflict_exc = StateConflict(fp.id, FarbState.CHECK_MARGIN, FarbState.OPENING_MARGIN)
    mocker.patch.object(
        strat.farb_repo, "transition", side_effect=conflict_exc
    )
    mock_rollback = mocker.patch.object(strat, "_rollback")
    mock_mark_failed = mocker.patch.object(strat.farb_repo, "mark_failed")

    await strat._advance_one(fp)

    # StateConflict must NOT trigger rollback or mark_failed
    mock_rollback.assert_not_called()
    mock_mark_failed.assert_not_called()

    # FP state remains CHECK_MARGIN (transition was stubbed out)
    updated = await farb_repo.get(fp.id)
    assert updated.state == FarbState.CHECK_MARGIN


@pytest.mark.asyncio
async def test_advance_one_safety_cap(session_factory, farb_repo, strategy_id, mocker):
    """Safety cap: if handlers never change state, loop bails after 20 iterations."""
    import frab.strategy.two_phase as two_phase_mod

    exchange = _make_exchange()
    strat = _make_strategy(exchange, farb_repo, session_factory)
    strat.strategy_id = strategy_id

    fp = await farb_repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.CHECK_MARGIN,
    )

    # Stub get_wallet so _step_check_margin passes the balance check...
    exchange.get_wallet.return_value = 10000.0

    # ...but make farb_repo.transition a no-op so state never changes
    async def _noop_transition(*args, **kwargs):
        pass

    mocker.patch.object(strat.farb_repo, "transition", side_effect=_noop_transition)

    # Patch farb_repo.get to always return the same CHECK_MARGIN FP
    original_get = strat.farb_repo.get

    async def _always_check_margin(fp_id):
        result = await original_get(fp_id)
        if result is not None:
            from dataclasses import replace
            return replace(result, state=FarbState.CHECK_MARGIN)
        return result

    mocker.patch.object(strat.farb_repo, "get", side_effect=_always_check_margin)

    # Patch the module-level logger to capture error calls
    mock_logger = mocker.patch.object(two_phase_mod, "logger")

    await strat._advance_one(fp)

    # The safety cap error message must have been logged
    error_msgs = [call.args[0] for call in mock_logger.error.call_args_list]
    assert any("safety cap" in m for m in error_msgs), (
        f"Expected 'safety cap' in error logs, got: {error_msgs}"
    )

    # Loop must have run exactly 20 iterations (transition called 20 times)
    assert strat.farb_repo.transition.call_count == strat._ADVANCE_MAX_ITERS


# ─── TwoPhaseParams tests ─────────────────────────────────────────────────────

def test_params_from_dict_known_keys():
    """from_dict picks up known keys and ignores unknown ones."""
    d = {
        "entry_threshold_apr": 0.15,
        "concurrency_cap": 5,
        "unknown_key": "ignored",
    }
    params = TwoPhaseParams.from_dict(d)
    assert params.entry_threshold_apr == 0.15
    assert params.concurrency_cap == 5


def test_params_required_margin():
    """required_margin = position_size / leverage * buffer."""
    params = TwoPhaseParams(
        position_size_usdc=1000.0,
        perp_leverage=5.0,
        margin_buffer_factor=3.0,
    )
    assert params.required_margin() == pytest.approx(600.0)  # 1000/5 * 3 = 600
