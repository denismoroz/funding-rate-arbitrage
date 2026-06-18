"""Unit tests for PostBreakevenHandler.

Each test drives PostBreakevenHandler.evaluate_one directly with a mock
FarbRepo, so no DB fixture is needed — all dependencies are faked.

Key invariants under test:
  - in_profit=True always (Phase 2 uses phase2_exit_threshold)
  - The latch is one-way: NEVER transitions back to PRE_BREAKEVEN
  - Close fires when signal < phase2_exit_threshold
  - StateConflict is swallowed
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from frab.domain import FarbPosition, FarbState
from frab.repo.farb_repo import StateConflict
from frab.coin_registry import RegistryAwareSettings
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states.post_breakeven import PostBreakevenHandler

_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC
_OPENED_MS = _NOW_MS - 100 * 3_600_000  # opened 100 h ago


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        entry_threshold_apr=0.10,
        phase2_exit_threshold=-0.10,
        base_min_hold_hours=24,
        cap_min_hold_hours=720,
        safety_mult=5.0,
        signal_window_hours=3,
        concurrency_cap=1,
        position_size_usdc=1000.0,
        budget_cap_usdc=1000.0,
        margin_buffer_factor=0.0,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    defaults.update(overrides)
    return TwoPhaseParams(**defaults)


def _make_fp(*, state_data: dict | None = None) -> FarbPosition:
    return FarbPosition(
        id=1,
        strategy_id=1,
        coin="BTC",
        state=FarbState.POST_BREAKEVEN,
        state_data=state_data or {"opened_at_ms": _OPENED_MS},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime.fromtimestamp(_OPENED_MS / 1000, tz=timezone.utc),
        closed_at=None,
    )


def _make_handler(mocker, *, params=None, signal_value=None):
    if params is None:
        params = _make_params()
    farb_repo = mocker.AsyncMock()
    signal_computer = mocker.AsyncMock(spec=SignalComputer)
    signal_computer.compute.return_value = signal_value
    settings = mocker.MagicMock(spec=RegistryAwareSettings)
    coin_spec = mocker.MagicMock()
    coin_spec.leverage = 10
    settings.get_coin_spec.return_value = coin_spec
    handler = PostBreakevenHandler(
        strategy_id=1,
        farb_repo=farb_repo,
        params=params,
        signal_computer=signal_computer,
        settings=settings,
    )
    return handler, farb_repo, signal_computer


# ─── Close tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_closes_when_signal_below_phase2_threshold(mocker):
    """Signal < phase2_exit_threshold → POST→CLOSING_SHORT."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 50.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, params=params, signal_value=-0.50)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_awaited_once()
    kwargs = farb_repo.transition.call_args.kwargs
    assert kwargs["from_state"] == FarbState.POST_BREAKEVEN
    assert kwargs["to_state"] == FarbState.CLOSING_SHORT


@pytest.mark.asyncio
async def test_no_close_when_signal_above_threshold(mocker):
    """Signal > phase2_exit_threshold → no close; checkpoint state_data."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 50.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, params=params, signal_value=0.20)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_not_called()
    farb_repo.update_state_data.assert_awaited_once()


# ─── One-way latch invariant ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_never_returns_to_pre_breakeven(mocker):
    """Even if gross dips below fees, POST never transitions back to PRE_BREAKEVEN."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 2.0,  # dipped below fees
        "total_fees_paid": 10.0,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, params=params, signal_value=0.20)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    # No transition to PRE_BREAKEVEN
    for call in farb_repo.transition.call_args_list:
        assert call.kwargs.get("to_state") != FarbState.PRE_BREAKEVEN
    # Positive signal, no close → checkpoint
    farb_repo.update_state_data.assert_awaited_once()


# ─── StateConflict handling ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_conflict_is_swallowed(mocker):
    """StateConflict on POST→CLOSING_SHORT is caught; no re-raise."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 50.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, params=params, signal_value=-0.50)
    farb_repo.transition.side_effect = StateConflict(fp.id, FarbState.POST_BREAKEVEN, FarbState.CLOSING_SHORT)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)
    farb_repo.transition.assert_awaited_once()


# ─── Checkpoint tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_checkpoint_updates_consec_negative_hours(mocker):
    """No-exit path: consec_negative_hours is updated in checkpoint."""
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 50.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, signal_value=0.50)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.update_state_data.assert_awaited_once()
    sd = farb_repo.update_state_data.call_args.args[1]
    assert "consec_negative_hours" in sd
    assert sd["consec_negative_hours"] == 0  # reset because signal positive


@pytest.mark.asyncio
async def test_consec_negative_incremented_on_mild_negative_signal(mocker):
    """Mild negative signal above threshold → no close; consec_negative incremented."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 50.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 5,
    })
    # Signal is negative but above phase2_exit_threshold
    handler, farb_repo, _ = _make_handler(mocker, params=params, signal_value=-0.05)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_not_called()
    sd = farb_repo.update_state_data.call_args.args[1]
    assert sd["consec_negative_hours"] > 5
