"""Unit tests for OpeningShortState."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.constants import CoinMarginSpec, PERP_TAKER, SPOT_TAKER
from frab.domain import FarbPosition, FarbState, Instrument, Side
from frab.settings import Settings
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._base import StrategyContext
from frab.strategy.two_phase.states.opening_short import OpeningShortState


def _make_fp(state_data: dict | None = None) -> FarbPosition:
    return FarbPosition(
        id=30,
        strategy_id=1,
        coin="SOL",
        state=FarbState.OPENING_SHORT,
        state_data=state_data if state_data is not None else {},
        spot_position_id=44,
        perp_position_id=None,
        margin_position_id=11,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        coins=["SOL"],
        position_size_usdc=1000.0,
        margin_buffer_factor=3.0,
        budget_cap_usdc=10000.0,
        concurrency_cap=3,
    )
    defaults.update(overrides)
    return TwoPhaseParams(**defaults)


def _make_settings(mocker, coin="SOL", leverage=5, maint_ratio=0.025):
    settings = mocker.MagicMock(spec=Settings)
    settings.get_coin_spec.return_value = CoinMarginSpec(leverage=leverage, maint_ratio=maint_ratio)
    return settings


def _make_exchange(mocker) -> object:
    exchange = mocker.AsyncMock()
    exchange.round_qty_to_nearest = mocker.AsyncMock(side_effect=lambda coin, qty: qty)
    return exchange


def _make_ctx(mocker, *, exchange=None, farb_repo=None, params=None, settings=None, event_bus=None) -> StrategyContext:
    return StrategyContext(
        exchange=exchange or _make_exchange(mocker),
        farb_repo=farb_repo or mocker.AsyncMock(),
        params=params or _make_params(),
        session_factory=mocker.MagicMock(),
        settings=settings or _make_settings(mocker),
        event_bus=event_bus,
    )


@pytest.mark.asyncio
async def test_opening_short_happy_path(mocker):
    """spot_qty from state_data → SHORT PERP OpenRequest, set_leg PERP, transition to OPEN."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)
    spec = settings.get_coin_spec("SOL")
    size_usdc = params.compute_size_for("SOL", settings)
    spot_qty = 10.0

    perp_pos = mocker.MagicMock()
    perp_pos.id = 55
    perp_pos.qty = 10.0
    perp_pos.entry_price = 22.5

    exchange = _make_exchange(mocker)
    exchange.open_position.return_value = perp_pos

    farb_repo = mocker.AsyncMock()
    farb_repo.set_leg = mocker.AsyncMock()
    farb_repo.transition = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = OpeningShortState(ctx)
    fp = _make_fp(state_data={"spot_qty": spot_qty, "spot_entry_price": 22.0, "target_signal_apr": 0.15})

    result = await state.execute(fp)

    assert result == FarbState.OPEN

    # OpenRequest check
    open_req = exchange.open_position.await_args.args[0]
    assert open_req.instrument == Instrument.PERP
    assert open_req.side == Side.SHORT
    assert open_req.qty == spot_qty
    assert open_req.leverage == spec.leverage

    # set_leg PERP
    farb_repo.set_leg.assert_awaited_once()
    set_leg_kwargs = farb_repo.set_leg.await_args.kwargs
    assert set_leg_kwargs["instrument"] == Instrument.PERP
    assert set_leg_kwargs["position_id"] == 55

    # transition to OPEN with required state_data keys
    trans_kwargs = farb_repo.transition.await_args.kwargs
    assert trans_kwargs["from_state"] == FarbState.OPENING_SHORT
    assert trans_kwargs["to_state"] == FarbState.OPEN
    sd = trans_kwargs["state_data"]
    assert sd["gross_funding_so_far"] == 0.0
    assert sd["consec_negative_hours"] == 0
    assert "position_min_hold_hours" in sd
    assert "opened_at_ms" in sd
    assert sd["leverage"] == spec.leverage
    expected_fees = size_usdc * (PERP_TAKER + SPOT_TAKER) * 2
    assert abs(sd["total_fees_paid"] - expected_fees) < 1e-9


@pytest.mark.asyncio
async def test_opening_short_publishes_farb_opened_event(mocker):
    """After successful transition, farb.opened INFO event is published."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)

    perp_pos = mocker.MagicMock()
    perp_pos.id = 60
    perp_pos.qty = 5.0
    perp_pos.entry_price = 200.0

    exchange = _make_exchange(mocker)
    exchange.open_position.return_value = perp_pos

    farb_repo = mocker.AsyncMock()

    event_bus = mocker.AsyncMock()
    event_bus.publish = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings, event_bus=event_bus)
    state = OpeningShortState(ctx)
    fp = _make_fp(state_data={"spot_qty": 5.0, "spot_entry_price": 199.0})

    await state.execute(fp)

    event_bus.publish.assert_awaited_once()
    published = event_bus.publish.await_args.args[0]
    assert published.level == "INFO"
    assert published.kind == "farb.opened"
    assert published.payload_json["farb_position_id"] == fp.id
    assert published.payload_json["coin"] == fp.coin


@pytest.mark.asyncio
async def test_opening_short_no_event_bus(mocker):
    """event_bus=None → no exception, no publish attempt."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)

    perp_pos = mocker.MagicMock()
    perp_pos.id = 70
    perp_pos.qty = 3.0
    perp_pos.entry_price = 300.0

    exchange = _make_exchange(mocker)
    exchange.open_position.return_value = perp_pos

    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings, event_bus=None)
    state = OpeningShortState(ctx)
    fp = _make_fp(state_data={"spot_qty": 3.0})

    result = await state.execute(fp)
    assert result == FarbState.OPEN


@pytest.mark.asyncio
async def test_opening_short_fallback_recomputes_qty_from_quote(mocker):
    """If spot_qty not in state_data, recompute from get_quote using compute_size_for."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)
    mark_price = 200.0
    size_usdc = params.compute_size_for("SOL", settings)

    quote = mocker.MagicMock()
    quote.spot = None
    quote.mark = mark_price

    perp_pos = mocker.MagicMock()
    perp_pos.id = 80
    perp_pos.qty = size_usdc / mark_price
    perp_pos.entry_price = mark_price

    exchange = _make_exchange(mocker)
    exchange.get_quote.return_value = quote
    exchange.open_position.return_value = perp_pos

    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = OpeningShortState(ctx)
    fp = _make_fp(state_data={})  # no spot_qty

    result = await state.execute(fp)

    assert result == FarbState.OPEN
    exchange.get_quote.assert_awaited_once_with(fp.coin)
    open_req = exchange.open_position.await_args.args[0]
    expected = size_usdc / mark_price
    assert abs(open_req.qty - expected) < 1e-9


@pytest.mark.asyncio
async def test_perp_hedge_uses_round_qty_to_nearest(mocker):
    """Perp hedge leg must size with HALF_UP (not FLOOR) so the short matches
    the spot wallet balance precisely. Regression guard for the dust-residual
    fix originally introduced in commit daa0141 and lost during the two_phase
    state-machine refactor (May 2026).

    For a spot delta of 0.000149895 BTC (post-fee balance), the perp short
    must round to 0.00015, not 0.00014.
    """
    params = _make_params(coins=["BTC"])
    settings = _make_settings(mocker, coin="BTC", leverage=3)

    spot_delta = 0.000149895  # post-fee wallet balance after spot BUY

    perp_pos = mocker.MagicMock()
    perp_pos.id = 99
    perp_pos.qty = 0.00015
    perp_pos.entry_price = 77000.0

    exchange = _make_exchange(mocker)
    exchange.round_qty_to_nearest = mocker.AsyncMock(return_value=0.00015)
    exchange.open_position.return_value = perp_pos

    farb_repo = mocker.AsyncMock()

    fp = FarbPosition(
        id=31,
        strategy_id=1,
        coin="BTC",
        state=FarbState.OPENING_SHORT,
        state_data={"spot_qty": spot_delta, "spot_entry_price": 77000.0},
        spot_position_id=50,
        perp_position_id=None,
        margin_position_id=12,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = OpeningShortState(ctx)

    await state.execute(fp)

    exchange.round_qty_to_nearest.assert_awaited_once_with("BTC", pytest.approx(spot_delta))
    open_req = exchange.open_position.await_args.args[0]
    assert open_req.qty == pytest.approx(0.00015)
