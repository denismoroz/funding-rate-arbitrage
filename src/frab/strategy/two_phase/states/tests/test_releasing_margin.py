"""Unit tests for ReleasingMarginState."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.domain import FarbPosition, FarbState
from frab.settings import Settings
from frab.strategy.two_phase.states._base import StrategyContext
from frab.strategy.two_phase.states.releasing_margin import ReleasingMarginState
from frab.strategy.two_phase.params import TwoPhaseParams


def _make_fp(*, margin_position_id: int | None) -> FarbPosition:
    return FarbPosition(
        id=60,
        strategy_id=1,
        coin="AVAX",
        state=FarbState.RELEASING_MARGIN,
        state_data={},
        spot_position_id=22,
        perp_position_id=77,
        margin_position_id=margin_position_id,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_params() -> TwoPhaseParams:
    return TwoPhaseParams(position_size_usdc=1000.0, margin_buffer_factor=3.0)


def _make_ctx(mocker, *, exchange=None, farb_repo=None, session_factory=None) -> StrategyContext:
    return StrategyContext(
        exchange=exchange or mocker.AsyncMock(),
        farb_repo=farb_repo or mocker.AsyncMock(),
        params=_make_params(),
        session_factory=session_factory or mocker.MagicMock(),
        settings=mocker.MagicMock(spec=Settings),
        event_bus=None,
    )


@pytest.mark.asyncio
async def test_releasing_margin_with_margin_position(mocker):
    """Has margin_position_id → load + close collateral, then mark_closed, returns None."""
    coll_pos = mocker.MagicMock()

    mock_load = mocker.patch(
        "frab.strategy.two_phase.states.releasing_margin.load_position",
        new_callable=mocker.AsyncMock,
    )
    mock_load.return_value = coll_pos

    exchange = mocker.AsyncMock()
    exchange.close_position.return_value = mocker.MagicMock()

    farb_repo = mocker.AsyncMock()
    farb_repo.mark_closed = mocker.AsyncMock()

    session_factory = mocker.MagicMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, session_factory=session_factory)
    state = ReleasingMarginState(ctx)
    fp = _make_fp(margin_position_id=11)

    result = await state.execute(fp)

    assert result is None
    mock_load.assert_awaited_once_with(session_factory, 11)
    exchange.close_position.assert_awaited_once_with(coll_pos)
    farb_repo.mark_closed.assert_awaited_once_with(fp.id)


@pytest.mark.asyncio
async def test_releasing_margin_without_margin_position(mocker):
    """margin_position_id is None → skip load/close, only mark_closed, returns None."""
    mock_load = mocker.patch(
        "frab.strategy.two_phase.states.releasing_margin.load_position",
        new_callable=mocker.AsyncMock,
    )

    exchange = mocker.AsyncMock()
    farb_repo = mocker.AsyncMock()
    farb_repo.mark_closed = mocker.AsyncMock()
    session_factory = mocker.MagicMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, session_factory=session_factory)
    state = ReleasingMarginState(ctx)
    fp = _make_fp(margin_position_id=None)

    result = await state.execute(fp)

    assert result is None
    mock_load.assert_not_awaited()
    exchange.close_position.assert_not_awaited()
    farb_repo.mark_closed.assert_awaited_once_with(fp.id)
