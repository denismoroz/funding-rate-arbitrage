"""Unit tests for ClosingShortState."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.states.closing_short import ClosingShortState


def _make_fp(state: FarbState = FarbState.CLOSING_SHORT, *, perp_position_id: int | None = 77) -> FarbPosition:
    return FarbPosition(
        id=40,
        strategy_id=1,
        coin="BTC",
        state=state,
        state_data={},
        spot_position_id=22,
        perp_position_id=perp_position_id,
        margin_position_id=11,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


@pytest.mark.asyncio
async def test_closing_short_happy_path(mocker):
    """load_position → close_position → transition to CLOSING_LONG, returns CLOSING_LONG."""
    perp_pos = mocker.MagicMock()

    mock_load = mocker.patch(
        "frab.strategy.two_phase.states.closing_short.load_position",
        new_callable=mocker.AsyncMock,
    )
    mock_load.return_value = perp_pos

    exchange = mocker.AsyncMock()
    exchange.close_position.return_value = mocker.MagicMock()

    farb_repo = mocker.AsyncMock()
    farb_repo.transition = mocker.AsyncMock()

    session_factory = mocker.MagicMock()

    state = ClosingShortState(
        exchange=exchange,
        farb_repo=farb_repo,
        session_factory=session_factory,
    )
    fp = _make_fp(perp_position_id=77)

    result = await state.execute(fp)

    assert result == FarbState.CLOSING_LONG

    mock_load.assert_awaited_once_with(session_factory, 77)
    exchange.close_position.assert_awaited_once_with(perp_pos)

    trans_kwargs = farb_repo.transition.await_args.kwargs
    assert trans_kwargs["from_state"] == FarbState.CLOSING_SHORT
    assert trans_kwargs["to_state"] == FarbState.CLOSING_LONG


@pytest.mark.asyncio
async def test_closing_short_raises_if_no_perp_position_id(mocker):
    """fp.perp_position_id is None → RuntimeError raised before any exchange call."""
    exchange = mocker.AsyncMock()
    farb_repo = mocker.AsyncMock()
    session_factory = mocker.MagicMock()

    state = ClosingShortState(
        exchange=exchange,
        farb_repo=farb_repo,
        session_factory=session_factory,
    )
    fp = _make_fp(perp_position_id=None)

    with pytest.raises(RuntimeError, match="no perp_position_id"):
        await state.execute(fp)

    exchange.close_position.assert_not_awaited()
    farb_repo.transition.assert_not_awaited()
