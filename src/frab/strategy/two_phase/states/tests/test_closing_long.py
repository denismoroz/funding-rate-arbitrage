"""Unit tests for ClosingLongState."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.states._base import StrategyContext
from frab.strategy.two_phase.states.closing_long import ClosingLongState
from frab.strategy.two_phase.params import TwoPhaseParams


def _make_fp(*, spot_position_id: int | None = 22, state_data: dict | None = None) -> FarbPosition:
    return FarbPosition(
        id=50,
        strategy_id=1,
        coin="ETH",
        state=FarbState.CLOSING_LONG,
        state_data=state_data if state_data is not None else {},
        spot_position_id=spot_position_id,
        perp_position_id=77,
        margin_position_id=11,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_params() -> TwoPhaseParams:
    return TwoPhaseParams(coins=["ETH"], position_size_usdc=1000.0, perp_leverage=5.0, margin_buffer_factor=3.0)


def _make_ctx(mocker, *, exchange=None, farb_repo=None, session_factory=None, event_bus=None) -> StrategyContext:
    return StrategyContext(
        exchange=exchange or mocker.AsyncMock(),
        farb_repo=farb_repo or mocker.AsyncMock(),
        params=_make_params(),
        session_factory=session_factory or mocker.MagicMock(),
        event_bus=event_bus,
    )


@pytest.mark.asyncio
async def test_closing_long_happy_path(mocker):
    """load_position → close_position → transition to RELEASING_MARGIN, publish farb.closed."""
    spot_pos = mocker.MagicMock()

    mock_load = mocker.patch(
        "frab.strategy.two_phase.states.closing_long.load_position",
        new_callable=mocker.AsyncMock,
    )
    mock_load.return_value = spot_pos

    exchange = mocker.AsyncMock()
    exchange.close_position.return_value = mocker.MagicMock()

    farb_repo = mocker.AsyncMock()
    farb_repo.transition = mocker.AsyncMock()

    event_bus = mocker.AsyncMock()
    event_bus.publish = mocker.AsyncMock()

    session_factory = mocker.MagicMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, session_factory=session_factory, event_bus=event_bus)
    state = ClosingLongState(ctx)
    fp = _make_fp(
        spot_position_id=22,
        state_data={"hours_in_position": 48, "exit_signal_apr": 0.05, "exit_decision": "phase2"},
    )

    result = await state.execute(fp)

    assert result == FarbState.RELEASING_MARGIN

    mock_load.assert_awaited_once_with(session_factory, 22)
    exchange.close_position.assert_awaited_once_with(spot_pos)

    trans_kwargs = farb_repo.transition.await_args.kwargs
    assert trans_kwargs["from_state"] == FarbState.CLOSING_LONG
    assert trans_kwargs["to_state"] == FarbState.RELEASING_MARGIN

    event_bus.publish.assert_awaited_once()
    published = event_bus.publish.await_args.args[0]
    assert published.level == "INFO"
    assert published.kind == "farb.closed"
    assert published.payload_json["farb_position_id"] == fp.id
    assert published.payload_json["coin"] == fp.coin


@pytest.mark.asyncio
async def test_closing_long_raises_if_no_spot_position_id(mocker):
    """fp.spot_position_id is None → RuntimeError raised before exchange call."""
    exchange = mocker.AsyncMock()
    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo)
    state = ClosingLongState(ctx)
    fp = _make_fp(spot_position_id=None)

    with pytest.raises(RuntimeError, match="no spot_position_id"):
        await state.execute(fp)

    exchange.close_position.assert_not_awaited()
    farb_repo.transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_long_no_event_bus(mocker):
    """event_bus=None → no exception, no publish attempt."""
    spot_pos = mocker.MagicMock()

    mock_load = mocker.patch(
        "frab.strategy.two_phase.states.closing_long.load_position",
        new_callable=mocker.AsyncMock,
    )
    mock_load.return_value = spot_pos

    exchange = mocker.AsyncMock()
    farb_repo = mocker.AsyncMock()
    session_factory = mocker.MagicMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, session_factory=session_factory, event_bus=None)
    state = ClosingLongState(ctx)
    fp = _make_fp(spot_position_id=22)

    result = await state.execute(fp)
    assert result == FarbState.RELEASING_MARGIN
