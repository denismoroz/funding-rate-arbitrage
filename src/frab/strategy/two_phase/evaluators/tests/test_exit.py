"""Unit tests for ExitEvaluator — fully mocked deps, no DB fixtures."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from frab.domain import FarbPosition, FarbState
from frab.repo.farb_repo import StateConflict
from frab.settings import Settings
from frab.strategy.two_phase.evaluators.exit import ExitEvaluator
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.params import TwoPhaseParams


_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC
# opened 100 hours before now to exceed any min_hold
_OPENED_MS = _NOW_MS - 100 * 3_600_000


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        coins=["BTC", "ETH"],
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


def _make_fp(*, coin: str = "BTC", state: FarbState = FarbState.PRE_BREAKEVEN,
             state_data: dict | None = None, id: int = 1) -> FarbPosition:
    return FarbPosition(
        id=id,
        strategy_id=1,
        coin=coin,
        state=state,
        state_data=state_data or {"opened_at_ms": _OPENED_MS},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime.fromtimestamp(_OPENED_MS / 1000, tz=timezone.utc),
        closed_at=None,
    )


def _make_evaluator(mocker, *, params=None, active_fps=None, signal_value=None):
    if params is None:
        params = _make_params()

    farb_repo = mocker.AsyncMock()
    farb_repo.list_active.return_value = active_fps or []

    signal_computer = mocker.AsyncMock(spec=SignalComputer)
    signal_computer.compute.return_value = signal_value

    # Stub settings: leverage=10
    settings = mocker.MagicMock(spec=Settings)
    coin_spec = mocker.MagicMock()
    coin_spec.leverage = 10
    settings.get_coin_spec.return_value = coin_spec

    evaluator = ExitEvaluator(
        strategy_id=1,
        farb_repo=farb_repo,
        params=params,
        signal_computer=signal_computer,
        settings=settings,
    )
    return evaluator, farb_repo, signal_computer


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_iterates_active_and_calls_evaluate_one(mocker):
    """evaluate() calls evaluate_one for each active (PRE/POST) position."""
    fp1 = _make_fp(coin="BTC", id=1, state=FarbState.PRE_BREAKEVEN)
    fp2 = _make_fp(coin="ETH", id=2, state=FarbState.POST_BREAKEVEN)

    evaluator, farb_repo, _ = _make_evaluator(
        mocker, active_fps=[fp1, fp2], signal_value=None
    )
    # spy on evaluate_one to track calls
    spy = mocker.patch.object(evaluator, "evaluate_one", wraps=evaluator.evaluate_one)

    await evaluator.evaluate(now_ms=_NOW_MS)

    assert spy.call_count == 2
    called_fps = [call.args[0] for call in spy.call_args_list]
    assert fp1 in called_fps
    assert fp2 in called_fps


@pytest.mark.asyncio
async def test_evaluate_one_pre_triggers_exit_when_signal_below_threshold(mocker):
    """evaluate_one on PRE_BREAKEVEN transitions to CLOSING_SHORT when decide returns non-NONE."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(
        state=FarbState.PRE_BREAKEVEN,
        state_data={
            "opened_at_ms": _OPENED_MS,
            "consec_negative_hours": 200,
            "gross_funding_so_far": 0.0,
            "total_fees_paid": 10.0,
        },
    )

    evaluator, farb_repo, signal_computer = _make_evaluator(
        mocker,
        params=params,
        active_fps=[fp],
        signal_value=-0.50,  # well below neg_stop threshold → CLOSE_PRE_BE_NEGSTOP
    )

    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_awaited_once()
    call_kwargs = farb_repo.transition.call_args.kwargs
    assert call_kwargs["to_state"] == FarbState.CLOSING_SHORT
    assert call_kwargs["from_state"] == FarbState.PRE_BREAKEVEN


@pytest.mark.asyncio
async def test_evaluate_one_pre_swallows_state_conflict(mocker):
    """StateConflict on PRE→CLOSING_SHORT is swallowed — no re-raise."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(
        state=FarbState.PRE_BREAKEVEN,
        state_data={
            "opened_at_ms": _OPENED_MS,
            "consec_negative_hours": 200,
            "gross_funding_so_far": 0.0,
            "total_fees_paid": 10.0,
        },
    )

    evaluator, farb_repo, _ = _make_evaluator(
        mocker,
        params=params,
        active_fps=[fp],
        signal_value=-0.50,
    )
    farb_repo.transition.side_effect = StateConflict(fp.id, FarbState.PRE_BREAKEVEN, FarbState.CLOSING_SHORT)

    # Should not raise
    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)
    farb_repo.transition.assert_awaited_once()
    farb_repo.update_state_data.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_one_pre_updates_state_data_when_decision_is_none(mocker):
    """PRE_BREAKEVEN: decision==NONE → update_state_data with updated consec_negative_hours."""
    # Strong positive signal, fees not yet covered → stays in PRE, no exit
    fp = _make_fp(
        state=FarbState.PRE_BREAKEVEN,
        state_data={
            "opened_at_ms": _OPENED_MS,
            "consec_negative_hours": 0,
            "gross_funding_so_far": 0.0,
            "total_fees_paid": 10.0,  # not yet covered
        },
    )

    evaluator, farb_repo, _ = _make_evaluator(
        mocker,
        active_fps=[fp],
        signal_value=0.50,  # positive → no exit
    )

    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_not_called()
    farb_repo.update_state_data.assert_awaited_once()
    sd_arg = farb_repo.update_state_data.call_args.args[1]
    assert "consec_negative_hours" in sd_arg
    assert sd_arg["consec_negative_hours"] == 0  # reset because signal positive


@pytest.mark.asyncio
async def test_evaluate_one_pre_latches_to_post_when_gross_covers_fees(mocker):
    """PRE_BREAKEVEN: gross_funding >= total_fees → latch to POST_BREAKEVEN, no close."""
    fp = _make_fp(
        state=FarbState.PRE_BREAKEVEN,
        state_data={
            "opened_at_ms": _OPENED_MS,
            "consec_negative_hours": 0,
            "gross_funding_so_far": 10.0,
            "total_fees_paid": 10.0,  # exactly at breakeven
        },
    )

    evaluator, farb_repo, _ = _make_evaluator(
        mocker,
        active_fps=[fp],
        signal_value=0.20,
    )

    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)

    # Should transition PRE → POST, not PRE → CLOSING_SHORT
    farb_repo.transition.assert_awaited_once()
    call_kwargs = farb_repo.transition.call_args.kwargs
    assert call_kwargs["from_state"] == FarbState.PRE_BREAKEVEN
    assert call_kwargs["to_state"] == FarbState.POST_BREAKEVEN
    # update_state_data must NOT be called (latch returns immediately)
    farb_repo.update_state_data.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_one_post_closes_when_signal_below_phase2_threshold(mocker):
    """POST_BREAKEVEN: signal < phase2_exit_threshold → CLOSING_SHORT."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(
        state=FarbState.POST_BREAKEVEN,
        state_data={
            "opened_at_ms": _OPENED_MS,
            "consec_negative_hours": 0,
            "gross_funding_so_far": 50.0,
            "total_fees_paid": 10.0,
        },
    )

    evaluator, farb_repo, _ = _make_evaluator(
        mocker,
        params=params,
        active_fps=[fp],
        signal_value=-0.50,  # below -0.10 threshold → CLOSE_POST_BE
    )

    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_awaited_once()
    call_kwargs = farb_repo.transition.call_args.kwargs
    assert call_kwargs["from_state"] == FarbState.POST_BREAKEVEN
    assert call_kwargs["to_state"] == FarbState.CLOSING_SHORT


@pytest.mark.asyncio
async def test_evaluate_one_post_never_returns_to_pre(mocker):
    """POST_BREAKEVEN: even if gross dips below fees, NEVER transitions back to PRE."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(
        state=FarbState.POST_BREAKEVEN,
        state_data={
            "opened_at_ms": _OPENED_MS,
            "consec_negative_hours": 0,
            "gross_funding_so_far": 5.0,   # dipped below fees
            "total_fees_paid": 10.0,
        },
    )

    evaluator, farb_repo, _ = _make_evaluator(
        mocker,
        params=params,
        active_fps=[fp],
        signal_value=0.20,  # positive → no close
    )

    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)

    # No transition to PRE_BREAKEVEN (it's a one-way latch)
    for call in farb_repo.transition.call_args_list:
        assert call.kwargs.get("to_state") != FarbState.PRE_BREAKEVEN
    # Positive signal, no close → update_state_data checkpoint
    farb_repo.update_state_data.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_one_post_updates_state_data_when_no_exit(mocker):
    """POST_BREAKEVEN: strong positive signal → NONE → update_state_data."""
    fp = _make_fp(
        state=FarbState.POST_BREAKEVEN,
        state_data={
            "opened_at_ms": _OPENED_MS,
            "consec_negative_hours": 0,
            "gross_funding_so_far": 50.0,
            "total_fees_paid": 10.0,
        },
    )

    evaluator, farb_repo, _ = _make_evaluator(
        mocker,
        active_fps=[fp],
        signal_value=0.50,  # positive → no exit
    )

    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_not_called()
    farb_repo.update_state_data.assert_awaited_once()
    sd_arg = farb_repo.update_state_data.call_args.args[1]
    assert sd_arg["consec_negative_hours"] == 0
