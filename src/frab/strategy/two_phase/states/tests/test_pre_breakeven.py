"""Unit tests for PreBreakevenHandler.

Each test drives PreBreakevenHandler.evaluate_one directly with a mock
FarbRepo, so no DB fixture is needed — all dependencies are faked.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from frab.domain import FarbPosition, FarbState
from frab.repo.farb_repo import StateConflict
from frab.settings import Settings
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states.pre_breakeven import PreBreakevenHandler

_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC
_OPENED_MS = _NOW_MS - 100 * 3_600_000  # opened 100 h ago


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        coins=["BTC"],
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
        state=FarbState.PRE_BREAKEVEN,
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
    settings = mocker.MagicMock(spec=Settings)
    coin_spec = mocker.MagicMock()
    coin_spec.leverage = 10
    settings.get_coin_spec.return_value = coin_spec
    handler = PreBreakevenHandler(
        strategy_id=1,
        farb_repo=farb_repo,
        params=params,
        signal_computer=signal_computer,
        settings=settings,
    )
    return handler, farb_repo, signal_computer


# ─── Latch tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_latch_pre_to_post_when_gross_equals_fees(mocker):
    """gross_funding >= total_fees → transition PRE→POST_BREAKEVEN, return immediately."""
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 10.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, signal_value=0.20)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_awaited_once()
    kwargs = farb_repo.transition.call_args.kwargs
    assert kwargs["from_state"] == FarbState.PRE_BREAKEVEN
    assert kwargs["to_state"] == FarbState.POST_BREAKEVEN
    # Must NOT also call update_state_data (latch returns after transition)
    farb_repo.update_state_data.assert_not_called()


@pytest.mark.asyncio
async def test_latch_pre_to_post_when_gross_exceeds_fees(mocker):
    """gross_funding > total_fees → also latches PRE→POST."""
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 50.0,
        "total_fees_paid": 4.2,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, signal_value=0.10)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    kwargs = farb_repo.transition.call_args.kwargs
    assert kwargs["to_state"] == FarbState.POST_BREAKEVEN


@pytest.mark.asyncio
async def test_latch_state_conflict_is_swallowed(mocker):
    """StateConflict on PRE→POST latch is caught silently; no re-raise."""
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 10.0,
        "total_fees_paid": 4.0,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, signal_value=0.10)
    farb_repo.transition.side_effect = StateConflict(fp.id, FarbState.PRE_BREAKEVEN, FarbState.POST_BREAKEVEN)

    # Must not raise
    await handler.evaluate_one(fp, now_ms=_NOW_MS)
    farb_repo.transition.assert_awaited_once()


# ─── Exit (Phase 1) tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_negstop_triggers_close(mocker):
    """Sustained negative signal (consec_negative_hours >> patience) → CLOSING_SHORT."""
    params = _make_params(phase1_negative_patience=72, base_min_hold_hours=0)
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 0.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 200,  # >> patience=72
    })
    handler, farb_repo, _ = _make_handler(mocker, params=params, signal_value=-0.50)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_awaited_once()
    kwargs = farb_repo.transition.call_args.kwargs
    assert kwargs["from_state"] == FarbState.PRE_BREAKEVEN
    assert kwargs["to_state"] == FarbState.CLOSING_SHORT


@pytest.mark.asyncio
async def test_no_exit_before_min_hold(mocker):
    """Negative signal that does not meet negstop threshold: no exit before min_hold elapses.

    CLOSE_PRE_BE_NEGSTOP bypasses min_hold when signal < neg_stop_threshold (-0.15)
    AND consec_neg >= neg_stop_patience (default 6). So to test the min_hold gate
    we use a mild negative signal (-0.05, above -0.15) that doesn't trigger negstop.
    """
    params = _make_params(base_min_hold_hours=200, phase1_negative_patience=72)
    # Opened only 10 h ago — well below min_hold=200
    recent_opened_ms = _NOW_MS - 10 * 3_600_000
    fp = _make_fp(state_data={
        "opened_at_ms": recent_opened_ms,
        "gross_funding_so_far": 0.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 5,
        "position_min_hold_hours": 200,
    })
    # Mild negative signal — does NOT meet neg_stop_threshold=-0.15, so negstop won't fire
    handler, farb_repo, _ = _make_handler(mocker, params=params, signal_value=-0.05)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    # No close triggered; state_data updated with checkpoint
    farb_repo.transition.assert_not_called()
    farb_repo.update_state_data.assert_awaited_once()


@pytest.mark.asyncio
async def test_positive_signal_checkpoints_only(mocker):
    """Strong positive signal → NONE decision → update_state_data, no transition."""
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 0.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 0,
    })
    handler, farb_repo, _ = _make_handler(mocker, signal_value=0.50)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_not_called()
    farb_repo.update_state_data.assert_awaited_once()
    sd = farb_repo.update_state_data.call_args.args[1]
    assert "consec_negative_hours" in sd


@pytest.mark.asyncio
async def test_exit_state_conflict_is_swallowed(mocker):
    """StateConflict on PRE→CLOSING_SHORT is swallowed."""
    params = _make_params(phase1_negative_patience=72, base_min_hold_hours=0)
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 0.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 200,
    })
    handler, farb_repo, _ = _make_handler(mocker, params=params, signal_value=-0.50)
    farb_repo.transition.side_effect = StateConflict(fp.id, FarbState.PRE_BREAKEVEN, FarbState.CLOSING_SHORT)

    await handler.evaluate_one(fp, now_ms=_NOW_MS)
    # No raise; transition was attempted
    farb_repo.transition.assert_awaited_once()


@pytest.mark.asyncio
async def test_consec_negative_incremented_on_negative_signal(mocker):
    """Negative signal → consec_negative_hours incremented in checkpoint."""
    fp = _make_fp(state_data={
        "opened_at_ms": _OPENED_MS,
        "gross_funding_so_far": 0.0,
        "total_fees_paid": 10.0,
        "consec_negative_hours": 3,
        "position_min_hold_hours": 200,  # large min_hold so no exit fires
    })
    handler, farb_repo, _ = _make_handler(mocker, signal_value=-0.05)  # negative but mild

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    sd = farb_repo.update_state_data.call_args.args[1]
    # Counter should have increased from 3
    assert sd["consec_negative_hours"] > 3
