"""Unit tests for OpeningLongState."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.constants import CoinMarginSpec
from frab.domain import FarbPosition, FarbState, Instrument
from frab.exchanges.protocol import WalletKind
from frab.coin_registry import RegistryAwareSettings
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._base import StrategyContext
from frab.strategy.two_phase.states.opening_long import OpeningLongState


def _make_fp(state_data: dict | None = None) -> FarbPosition:
    return FarbPosition(
        id=20,
        strategy_id=1,
        coin="BTC",
        state=FarbState.OPENING_LONG,
        state_data=state_data if state_data is not None else {},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        position_size_usdc=1000.0,
        margin_buffer_factor=3.0,
        budget_cap_usdc=10000.0,
        concurrency_cap=3,
    )
    defaults.update(overrides)
    return TwoPhaseParams(**defaults)


def _make_settings(mocker, coin="BTC", leverage=5, maint_ratio=0.025):
    settings = mocker.MagicMock(spec=RegistryAwareSettings)
    settings.get_coin_spec.return_value = CoinMarginSpec(leverage=leverage, maint_ratio=maint_ratio)
    return settings


def _make_ctx(mocker, *, exchange=None, farb_repo=None, params=None, settings=None) -> StrategyContext:
    return StrategyContext(
        exchange=exchange or mocker.AsyncMock(),
        farb_repo=farb_repo or mocker.AsyncMock(),
        params=params or _make_params(),
        session_factory=mocker.MagicMock(),
        settings=settings or _make_settings(mocker),
        event_bus=None,
    )


@pytest.mark.asyncio
async def test_opening_long_happy_path_uses_spot_price(mocker):
    """get_quote returns spot price → qty = size_usdc/spot, set_leg SPOT, transition to OPENING_SHORT."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)
    spot_price = 50000.0
    size_usdc = params.compute_size_for("BTC", settings)
    expected_qty = size_usdc / spot_price

    quote = mocker.MagicMock()
    quote.spot = spot_price
    quote.mark = 51000.0  # should not be used when spot is available

    filled_pos = mocker.MagicMock()
    filled_pos.id = 77
    filled_pos.qty = 0.019  # HL-floored actual fill
    filled_pos.entry_price = 50010.0

    exchange = mocker.AsyncMock()
    exchange.get_quote.return_value = quote
    exchange.open_position.return_value = filled_pos
    # Wallet matches fill qty (no prior dust) → spot_qty = max(0.019, 0.019) = 0.019
    exchange.get_wallet = mocker.AsyncMock(return_value=filled_pos.qty)

    farb_repo = mocker.AsyncMock()
    farb_repo.set_leg = mocker.AsyncMock()
    farb_repo.transition = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = OpeningLongState(ctx)
    fp = _make_fp()

    result = await state.execute(fp)

    assert result == FarbState.OPENING_SHORT

    # open_position called with correct qty
    open_req = exchange.open_position.await_args.args[0]
    assert abs(open_req.qty - expected_qty) < 1e-9

    # get_wallet called with the coin and SPOT kind
    exchange.get_wallet.assert_awaited_once_with(fp.coin, WalletKind.SPOT)

    # set_leg called with SPOT and the filled position id
    farb_repo.set_leg.assert_awaited_once()
    set_leg_kwargs = farb_repo.set_leg.await_args.kwargs
    assert set_leg_kwargs["instrument"] == Instrument.SPOT
    assert set_leg_kwargs["position_id"] == 77

    # transition to OPENING_SHORT with wallet-anchored qty/price
    farb_repo.transition.assert_awaited_once()
    trans_kwargs = farb_repo.transition.await_args.kwargs
    assert trans_kwargs["from_state"] == FarbState.OPENING_LONG
    assert trans_kwargs["to_state"] == FarbState.OPENING_SHORT
    assert trans_kwargs["state_data"]["spot_qty"] == filled_pos.qty  # max(pos.qty, hl_balance) = pos.qty
    assert trans_kwargs["state_data"]["spot_entry_price"] == filled_pos.entry_price


@pytest.mark.asyncio
async def test_opening_long_falls_back_to_mark_when_spot_is_none(mocker):
    """If quote.spot is None, fall back to quote.mark for price."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)
    mark_price = 48000.0
    size_usdc = params.compute_size_for("BTC", settings)

    quote = mocker.MagicMock()
    quote.spot = None
    quote.mark = mark_price

    filled_pos = mocker.MagicMock()
    filled_pos.id = 88
    filled_pos.qty = 0.0208
    filled_pos.entry_price = 47990.0

    exchange = mocker.AsyncMock()
    exchange.get_quote.return_value = quote
    exchange.open_position.return_value = filled_pos
    exchange.get_wallet = mocker.AsyncMock(return_value=filled_pos.qty)

    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = OpeningLongState(ctx)
    fp = _make_fp()

    result = await state.execute(fp)

    assert result == FarbState.OPENING_SHORT

    open_req = exchange.open_position.await_args.args[0]
    expected_qty = size_usdc / mark_price
    assert abs(open_req.qty - expected_qty) < 1e-9


@pytest.mark.asyncio
async def test_opening_long_preserves_existing_state_data(mocker):
    """Existing state_data keys are preserved in the transition."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)

    quote = mocker.MagicMock()
    quote.spot = 40000.0
    quote.mark = 40000.0

    filled_pos = mocker.MagicMock()
    filled_pos.id = 5
    filled_pos.qty = 0.025
    filled_pos.entry_price = 40001.0

    exchange = mocker.AsyncMock()
    exchange.get_quote.return_value = quote
    exchange.open_position.return_value = filled_pos
    exchange.get_wallet = mocker.AsyncMock(return_value=filled_pos.qty)

    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = OpeningLongState(ctx)
    fp = _make_fp(state_data={"target_signal_apr": 0.30, "required_margin": 600.0})

    await state.execute(fp)

    merged = farb_repo.transition.await_args.kwargs["state_data"]
    assert merged["target_signal_apr"] == 0.30
    assert merged["required_margin"] == 600.0
    assert merged["spot_qty"] == filled_pos.qty


@pytest.mark.asyncio
async def test_opening_long_dust_absorption_uses_hl_balance_when_larger(mocker):
    """When HL wallet has more than pos.qty (dust from prior close), spot_qty = hl_balance."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)

    quote = mocker.MagicMock()
    quote.spot = 50000.0
    quote.mark = 50000.0

    filled_pos = mocker.MagicMock()
    filled_pos.id = 10
    filled_pos.qty = 0.019      # fill qty from this open
    filled_pos.entry_price = 50010.0

    # Wallet has 0.02001 — prior dust accumulated from previous partial close
    hl_balance = 0.02001

    exchange = mocker.AsyncMock()
    exchange.get_quote.return_value = quote
    exchange.open_position.return_value = filled_pos
    exchange.get_wallet = mocker.AsyncMock(return_value=hl_balance)

    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = OpeningLongState(ctx)
    fp = _make_fp()

    await state.execute(fp)

    trans_kwargs = farb_repo.transition.await_args.kwargs
    # Wallet > pos.qty → use wallet balance so the perp short covers the dust too
    assert trans_kwargs["state_data"]["spot_qty"] == hl_balance


@pytest.mark.asyncio
async def test_opening_long_defensive_max_uses_pos_qty_when_balance_lower(mocker):
    """Defensive: if HL balance < pos.qty (transient pre-settlement), spot_qty = pos.qty."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)

    quote = mocker.MagicMock()
    quote.spot = 50000.0
    quote.mark = 50000.0

    filled_pos = mocker.MagicMock()
    filled_pos.id = 11
    filled_pos.qty = 0.019
    filled_pos.entry_price = 50010.0

    # Wallet reports less than fill (transient, shouldn't happen normally)
    hl_balance = 0.018

    exchange = mocker.AsyncMock()
    exchange.get_quote.return_value = quote
    exchange.open_position.return_value = filled_pos
    exchange.get_wallet = mocker.AsyncMock(return_value=hl_balance)

    farb_repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = OpeningLongState(ctx)
    fp = _make_fp()

    await state.execute(fp)

    trans_kwargs = farb_repo.transition.await_args.kwargs
    # pos.qty > hl_balance → use pos.qty (defensive max)
    assert trans_kwargs["state_data"]["spot_qty"] == filled_pos.qty
