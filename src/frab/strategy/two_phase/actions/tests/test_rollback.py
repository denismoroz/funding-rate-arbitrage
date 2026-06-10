"""Unit tests for RollbackAction — fully mocked deps, no DB fixtures."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from frab.domain import FarbPosition, FarbState
from frab.exchanges.protocol import WalletKind
from frab.strategy.two_phase.actions.rollback import RollbackAction
from frab.strategy.two_phase.params import TwoPhaseParams


_NOW_MS = 1_704_067_200_000


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        position_size_usdc=1000.0,
        margin_buffer_factor=3.0,
    )
    defaults.update(overrides)
    return TwoPhaseParams(**defaults)


def _make_fp(*, id: int = 1, state: FarbState = FarbState.PRE_BREAKEVEN,
             spot_position_id: int | None = None,
             perp_position_id: int | None = None,
             state_data: dict | None = None) -> FarbPosition:
    return FarbPosition(
        id=id,
        strategy_id=1,
        coin="BTC",
        state=state,
        state_data=state_data or {},
        spot_position_id=spot_position_id,
        perp_position_id=perp_position_id,
        margin_position_id=None,
        opened_at=datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc),
        closed_at=None,
    )


def _make_rollback(mocker, *, params=None):
    exchange = mocker.AsyncMock()
    sf = mocker.MagicMock()
    if params is None:
        params = _make_params()
    rollback = RollbackAction(exchange=exchange, session_factory=sf, params=params)
    return rollback, exchange


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_opening_short_with_spot_id_closes_spot_position(mocker):
    """partial_state=OPENING_SHORT with spot_position_id set → calls exchange.close_position."""
    fp = _make_fp(spot_position_id=99, state=FarbState.OPENING_SHORT)
    rollback, exchange = _make_rollback(mocker)

    fake_spot_pos = mocker.MagicMock()
    mocker.patch(
        "frab.strategy.two_phase.actions.rollback.load_position",
        new_callable=mocker.AsyncMock,
        return_value=fake_spot_pos,
    )

    await rollback.execute(fp, partial_state=FarbState.OPENING_SHORT, error=RuntimeError("test"))

    exchange.close_position.assert_awaited_once_with(fake_spot_pos)
    exchange.transfer.assert_not_called()


@pytest.mark.asyncio
async def test_opening_long_transfers_margin_back_to_spot(mocker):
    """partial_state=OPENING_LONG → calls exchange.transfer with required margin from state_data."""
    required = 600.0
    fp = _make_fp(
        state=FarbState.OPENING_LONG,
        state_data={"required_margin": required},
    )
    rollback, exchange = _make_rollback(mocker)

    mocker.patch(
        "frab.strategy.two_phase.actions.rollback.load_position",
        new_callable=mocker.AsyncMock,
    )

    await rollback.execute(fp, partial_state=FarbState.OPENING_LONG, error=RuntimeError("test"))

    exchange.transfer.assert_awaited_once_with("USDC", required, WalletKind.PERP, WalletKind.SPOT)
    exchange.close_position.assert_not_called()


@pytest.mark.asyncio
async def test_closing_states_log_error_and_do_not_call_exchange(mocker):
    """partial_state=CLOSING_SHORT or CLOSING_LONG → no exchange calls."""
    fp_short = _make_fp(state=FarbState.CLOSING_SHORT)
    fp_long = _make_fp(state=FarbState.CLOSING_LONG)

    for fp, partial_state in [
        (fp_short, FarbState.CLOSING_SHORT),
        (fp_long, FarbState.CLOSING_LONG),
    ]:
        rollback, exchange = _make_rollback(mocker)

        await rollback.execute(fp, partial_state=partial_state, error=RuntimeError("close err"))

        exchange.close_position.assert_not_called()
        exchange.transfer.assert_not_called()


@pytest.mark.asyncio
async def test_inner_exception_is_swallowed_and_not_reraised(mocker):
    """If close_position raises, the error is logged but NOT re-raised."""
    fp = _make_fp(spot_position_id=77, state=FarbState.OPENING_SHORT)
    rollback, exchange = _make_rollback(mocker)

    mocker.patch(
        "frab.strategy.two_phase.actions.rollback.load_position",
        new_callable=mocker.AsyncMock,
        return_value=mocker.MagicMock(),
    )
    exchange.close_position.side_effect = RuntimeError("exchange blew up")

    # Should not raise
    await rollback.execute(fp, partial_state=FarbState.OPENING_SHORT, error=RuntimeError("original"))

    exchange.close_position.assert_awaited_once()
