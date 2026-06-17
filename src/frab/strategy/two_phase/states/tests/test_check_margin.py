"""Unit tests for CheckMarginState."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.constants import CoinMarginSpec
from frab.domain import FarbPosition, FarbState
from frab.coin_registry import RegistryAwareSettings
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._base import StrategyContext
from frab.strategy.two_phase.states.check_margin import CheckMarginState


def _make_fp(state_data: dict | None = None) -> FarbPosition:
    return FarbPosition(
        id=42,
        strategy_id=1,
        coin="BTC",
        state=FarbState.CHECK_MARGIN,
        state_data=state_data if state_data is not None else {},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        coins=["BTC"],
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


def _make_ctx(mocker, *, exchange=None, farb_repo=None, params=None, settings=None, event_bus=None) -> StrategyContext:
    return StrategyContext(
        exchange=exchange or mocker.AsyncMock(),
        farb_repo=farb_repo or mocker.AsyncMock(),
        params=params or _make_params(),
        session_factory=mocker.MagicMock(),
        settings=settings or _make_settings(mocker),
        event_bus=event_bus,
    )


@pytest.mark.asyncio
async def test_check_margin_sufficient_balance_transitions(mocker):
    """balance >= required → transition to OPENING_MARGIN, return FarbState.OPENING_MARGIN."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)
    required = params.compute_required_margin_for("BTC", settings)

    exchange = mocker.AsyncMock()
    exchange.get_wallet.return_value = required + 100.0

    farb_repo = mocker.AsyncMock()
    farb_repo.transition = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = CheckMarginState(ctx)
    fp = _make_fp()

    result = await state.execute(fp)

    assert result == FarbState.OPENING_MARGIN
    farb_repo.transition.assert_awaited_once()
    call_kwargs = farb_repo.transition.await_args
    assert call_kwargs.kwargs["from_state"] == FarbState.CHECK_MARGIN
    assert call_kwargs.kwargs["to_state"] == FarbState.OPENING_MARGIN
    assert call_kwargs.kwargs["state_data"]["required_margin"] == required
    farb_repo.mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_margin_insufficient_balance_marks_failed(mocker):
    """balance < required → mark_failed called, WARNING event published, returns None."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)
    required = params.compute_required_margin_for("BTC", settings)

    exchange = mocker.AsyncMock()
    exchange.get_wallet.return_value = 1.0  # way below required

    farb_repo = mocker.AsyncMock()
    farb_repo.mark_failed = mocker.AsyncMock()

    event_bus = mocker.AsyncMock()
    event_bus.publish = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings, event_bus=event_bus)
    state = CheckMarginState(ctx)
    fp = _make_fp()

    result = await state.execute(fp)

    assert result is None
    farb_repo.mark_failed.assert_awaited_once_with(fp.id, reason=mocker.ANY)
    reason_arg = farb_repo.mark_failed.await_args.kwargs["reason"]
    assert "insufficient_margin" in reason_arg

    event_bus.publish.assert_awaited_once()
    published_event = event_bus.publish.await_args.args[0]
    assert published_event.level == "WARNING"
    assert published_event.kind == "farb.failed"

    farb_repo.transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_margin_no_event_bus_on_failure(mocker):
    """event_bus=None on failure path → no exception, no publish attempt."""
    params = _make_params()
    settings = _make_settings(mocker)

    exchange = mocker.AsyncMock()
    exchange.get_wallet.return_value = 0.0  # insufficient

    farb_repo = mocker.AsyncMock()
    farb_repo.mark_failed = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings, event_bus=None)
    state = CheckMarginState(ctx)
    fp = _make_fp()

    result = await state.execute(fp)

    assert result is None
    farb_repo.mark_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_margin_state_data_merging(mocker):
    """Existing state_data keys are preserved; required_margin is added."""
    params = _make_params()
    settings = _make_settings(mocker, leverage=5)
    required = params.compute_required_margin_for("BTC", settings)

    exchange = mocker.AsyncMock()
    exchange.get_wallet.return_value = 99999.0  # plenty

    farb_repo = mocker.AsyncMock()
    farb_repo.transition = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, farb_repo=farb_repo, params=params, settings=settings)
    state = CheckMarginState(ctx)
    existing_data = {"target_signal_apr": 0.25, "entry_ts_ms": 1234567890}
    fp = _make_fp(state_data=existing_data)

    await state.execute(fp)

    call_kwargs = farb_repo.transition.await_args.kwargs
    merged = call_kwargs["state_data"]
    # Old keys preserved
    assert merged["target_signal_apr"] == 0.25
    assert merged["entry_ts_ms"] == 1234567890
    # New key added
    assert merged["required_margin"] == required
