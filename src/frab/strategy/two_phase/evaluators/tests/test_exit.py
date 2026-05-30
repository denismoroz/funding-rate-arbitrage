"""Unit tests for ExitEvaluator — fully mocked deps, no DB fixtures."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from frab.domain import FarbPosition, FarbState
from frab.repo.farb_repo import StateConflict
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
        concurrency_cap=3,
        position_size_usdc=1000.0,
        budget_cap_usdc=10000.0,
        margin_buffer_factor=3.0,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    defaults.update(overrides)
    return TwoPhaseParams(**defaults)


def _make_fp(*, coin: str = "BTC", state: FarbState = FarbState.OPEN,
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


def _make_evaluator(mocker, *, params=None, open_fps=None, signal_value=None):
    if params is None:
        params = _make_params()

    farb_repo = mocker.AsyncMock()
    farb_repo.list_open.return_value = open_fps or []

    signal_computer = mocker.AsyncMock(spec=SignalComputer)
    signal_computer.compute.return_value = signal_value

    evaluator = ExitEvaluator(
        strategy_id=1,
        farb_repo=farb_repo,
        params=params,
        signal_computer=signal_computer,
    )
    return evaluator, farb_repo, signal_computer


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_iterates_open_and_calls_evaluate_one(mocker):
    """evaluate() calls evaluate_one for each OPEN position."""
    fp1 = _make_fp(coin="BTC", id=1)
    fp2 = _make_fp(coin="ETH", id=2)

    evaluator, farb_repo, _ = _make_evaluator(
        mocker, open_fps=[fp1, fp2], signal_value=None
    )
    # spy on evaluate_one to track calls
    spy = mocker.patch.object(evaluator, "evaluate_one", wraps=evaluator.evaluate_one)

    await evaluator.evaluate(now_ms=_NOW_MS)

    assert spy.call_count == 2
    called_fps = [call.args[0] for call in spy.call_args_list]
    assert fp1 in called_fps
    assert fp2 in called_fps


@pytest.mark.asyncio
async def test_evaluate_one_triggers_exit_when_signal_below_threshold(mocker):
    """evaluate_one transitions to CLOSING_SHORT when decide_two_phase returns non-NONE."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(state_data={"opened_at_ms": _OPENED_MS, "consec_negative_hours": 200})

    evaluator, farb_repo, signal_computer = _make_evaluator(
        mocker,
        params=params,
        open_fps=[fp],
        signal_value=-0.50,  # well below phase2_exit_threshold=-0.10
    )

    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_awaited_once()
    call_kwargs = farb_repo.transition.call_args.kwargs
    assert call_kwargs["to_state"] == FarbState.CLOSING_SHORT


@pytest.mark.asyncio
async def test_evaluate_one_swallows_state_conflict(mocker):
    """StateConflict on transition is swallowed — no re-raise."""
    params = _make_params(phase2_exit_threshold=-0.10, base_min_hold_hours=0)
    fp = _make_fp(state_data={"opened_at_ms": _OPENED_MS, "consec_negative_hours": 200})

    evaluator, farb_repo, _ = _make_evaluator(
        mocker,
        params=params,
        open_fps=[fp],
        signal_value=-0.50,
    )
    farb_repo.transition.side_effect = StateConflict(fp.id, FarbState.OPEN, FarbState.CLOSING_SHORT)

    # Should not raise
    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)
    farb_repo.transition.assert_awaited_once()
    # update_state_data must NOT be called on StateConflict path
    farb_repo.update_state_data.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_one_updates_state_data_when_decision_is_none(mocker):
    """When decision == NONE, calls update_state_data with updated consec_negative_hours."""
    # Strong positive signal → NONE decision (no exit)
    fp = _make_fp(state_data={"opened_at_ms": _OPENED_MS, "consec_negative_hours": 0})

    evaluator, farb_repo, _ = _make_evaluator(
        mocker,
        open_fps=[fp],
        signal_value=0.50,  # positive → no exit
    )

    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)

    farb_repo.transition.assert_not_called()
    farb_repo.update_state_data.assert_awaited_once()
    sd_arg = farb_repo.update_state_data.call_args.args[1]
    # consec_negative_hours should be present and reset to 0 (since signal is positive)
    assert "consec_negative_hours" in sd_arg
    assert sd_arg["consec_negative_hours"] == 0
