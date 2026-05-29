"""Unit tests for StateMachine."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.state_machine import StateMachine


def _make_fp(state: FarbState) -> FarbPosition:
    return FarbPosition(
        id=1,
        strategy_id=1,
        coin="BTC",
        state=state,
        state_data={},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


@pytest.mark.asyncio
async def test_state_machine_registered_handler_called(mocker):
    """Registered handler for fp.state is called with fp; its return value is returned."""
    handler = mocker.AsyncMock()
    handler.execute.return_value = FarbState.OPENING_MARGIN

    sm = StateMachine({FarbState.CHECK_MARGIN: handler})
    fp = _make_fp(FarbState.CHECK_MARGIN)

    result = await sm.step(fp)

    assert result == FarbState.OPENING_MARGIN
    handler.execute.assert_awaited_once_with(fp)


@pytest.mark.asyncio
async def test_state_machine_unregistered_state_returns_none(mocker):
    """No handler registered for fp.state → returns None without raising."""
    sm = StateMachine({})  # no handlers at all
    fp = _make_fp(FarbState.OPENING_MARGIN)

    result = await sm.step(fp)

    assert result is None


@pytest.mark.asyncio
async def test_state_machine_handler_exception_propagates(mocker):
    """If the registered handler raises, StateMachine does not swallow the exception."""
    handler = mocker.AsyncMock()
    handler.execute.side_effect = RuntimeError("boom")

    sm = StateMachine({FarbState.CHECK_MARGIN: handler})
    fp = _make_fp(FarbState.CHECK_MARGIN)

    with pytest.raises(RuntimeError, match="boom"):
        await sm.step(fp)
