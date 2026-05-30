"""Unit tests for OpeningMarginState."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.domain import FarbPosition, FarbState, Instrument
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._base import StrategyContext
from frab.strategy.two_phase.states.opening_margin import OpeningMarginState


def _make_fp(state: FarbState = FarbState.OPENING_MARGIN, state_data: dict | None = None) -> FarbPosition:
    return FarbPosition(
        id=10,
        strategy_id=1,
        coin="ETH",
        state=state,
        state_data=state_data if state_data is not None else {},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        coins=["ETH"],
        position_size_usdc=1000.0,
        perp_leverage=5.0,
        margin_buffer_factor=3.0,
    )
    defaults.update(overrides)
    return TwoPhaseParams(**defaults)


def _make_position(id: int) -> object:
    """Minimal stand-in for a domain Position."""
    pos = object.__new__(object.__class__)
    # Use a simple namespace
    class _Pos:
        pass
    p = _Pos()
    p.id = id
    return p


def _make_ctx(mocker, *, exchange=None, farb_repo=None, params=None) -> StrategyContext:
    return StrategyContext(
        exchange=exchange or mocker.AsyncMock(),
        farb_repo=farb_repo or mocker.AsyncMock(),
        params=params or _make_params(),
        session_factory=mocker.MagicMock(),
        event_bus=None,
    )


@pytest.mark.asyncio
async def test_opening_margin_happy_path(mocker):
    """open_position → set_leg COLLATERAL → transition to OPENING_LONG, return OPENING_LONG."""
    params = _make_params()

    coll_pos = mocker.MagicMock()
    coll_pos.id = 99

    exchange = mocker.AsyncMock()
    exchange.open_position.return_value = coll_pos

    farb_repo = mocker.AsyncMock()
    farb_repo.set_leg = mocker.AsyncMock()
    farb_repo.transition = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params)
    state = OpeningMarginState(ctx)
    fp = _make_fp()

    result = await state.execute(fp)

    assert result == FarbState.OPENING_LONG

    # set_leg called with COLLATERAL and position_id=99
    farb_repo.set_leg.assert_awaited_once()
    set_leg_kwargs = farb_repo.set_leg.await_args.kwargs
    assert set_leg_kwargs["instrument"] == Instrument.COLLATERAL
    assert set_leg_kwargs["position_id"] == 99

    # transition from OPENING_MARGIN to OPENING_LONG
    farb_repo.transition.assert_awaited_once()
    trans_kwargs = farb_repo.transition.await_args.kwargs
    assert trans_kwargs["from_state"] == FarbState.OPENING_MARGIN
    assert trans_kwargs["to_state"] == FarbState.OPENING_LONG


@pytest.mark.asyncio
async def test_opening_margin_uses_required_margin_from_state_data(mocker):
    """If state_data has required_margin, use that instead of params.required_margin()."""
    params = _make_params()
    custom_required = 999.0

    coll_pos = mocker.MagicMock()
    coll_pos.id = 55

    exchange = mocker.AsyncMock()
    exchange.open_position.return_value = coll_pos

    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params)
    state = OpeningMarginState(ctx)
    fp = _make_fp(state_data={"required_margin": custom_required})

    await state.execute(fp)

    open_req = exchange.open_position.await_args.args[0]
    assert open_req.qty == custom_required


@pytest.mark.asyncio
async def test_opening_margin_falls_back_to_params_required_margin(mocker):
    """Without required_margin in state_data, uses params.required_margin()."""
    params = _make_params()
    expected_required = params.required_margin()

    coll_pos = mocker.MagicMock()
    coll_pos.id = 1

    exchange = mocker.AsyncMock()
    exchange.open_position.return_value = coll_pos

    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params)
    state = OpeningMarginState(ctx)
    fp = _make_fp(state_data={})

    await state.execute(fp)

    open_req = exchange.open_position.await_args.args[0]
    assert open_req.qty == expected_required
