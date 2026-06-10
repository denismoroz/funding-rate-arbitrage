"""Unit tests for EntryEvaluator — fully mocked deps, no DB fixtures."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.evaluators.entry import EntryEvaluator
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.params import TwoPhaseParams


_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC
_CURRENT_HOUR = _NOW_MS // 3_600_000


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        coins=["BTC", "ETH", "SOL"],
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


def _make_fp(*, coin: str = "BTC", state: FarbState = FarbState.PRE_BREAKEVEN,
             closed_at=None, id: int = 1) -> FarbPosition:
    return FarbPosition(
        id=id,
        strategy_id=1,
        coin=coin,
        state=state,
        state_data={},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc),
        closed_at=closed_at,
    )


def _make_evaluator(mocker, *, params=None, signal_side_effect=None,
                    non_terminal=None, by_coin_terminal=None,
                    by_coin_nonterminal=None):
    """Return (evaluator, farb_repo_mock, signal_computer_mock)."""
    if params is None:
        params = _make_params()

    farb_repo = mocker.AsyncMock()
    farb_repo.list_non_terminal.return_value = non_terminal or []

    def _by_coin(strategy_id, coin, include_terminal=False):
        if include_terminal:
            return (by_coin_terminal or {}).get(coin, [])
        return (by_coin_nonterminal or {}).get(coin, [])

    farb_repo.list_by_coin.side_effect = _by_coin

    signal_computer = mocker.AsyncMock(spec=SignalComputer)
    if signal_side_effect is not None:
        signal_computer.compute.side_effect = signal_side_effect
    else:
        signal_computer.compute.return_value = None

    evaluator = EntryEvaluator(
        strategy_id=1,
        farb_repo=farb_repo,
        params=params,
        signal_computer=signal_computer,
    )
    return evaluator, farb_repo, signal_computer


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skips_when_no_slots_available(mocker):
    """When non_terminal_count >= concurrency_cap, returns without creating FPs."""
    params = _make_params(concurrency_cap=2)
    # 2 non-terminal = 2, slots = 2 - 2 = 0
    non_terminal = [
        _make_fp(coin="BTC", state=FarbState.CHECK_MARGIN, id=1),
        _make_fp(coin="ETH", state=FarbState.PRE_BREAKEVEN, id=2),
    ]

    evaluator, farb_repo, _ = _make_evaluator(
        mocker, params=params, non_terminal=non_terminal
    )
    await evaluator.evaluate(now_ms=_NOW_MS, force_cooldown_bypass=False)
    farb_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_skips_coin_with_existing_nonterminal_position(mocker):
    """Coin already has a non-terminal position → skipped."""
    params = _make_params(coins=["BTC"], concurrency_cap=3)
    btc_pre = _make_fp(coin="BTC", state=FarbState.PRE_BREAKEVEN, id=1)

    evaluator, farb_repo, signal_computer = _make_evaluator(
        mocker,
        params=params,
        non_terminal=[],
        # list_by_coin non-terminal returns existing BTC position → skip
        by_coin_nonterminal={"BTC": [btc_pre]},
        by_coin_terminal={"BTC": []},
        signal_side_effect=[0.50],
    )
    await evaluator.evaluate(now_ms=_NOW_MS, force_cooldown_bypass=False)
    # existing non-terminal for BTC → no create
    farb_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_blocks_and_bypass_overrides(mocker):
    """Failed in same hour → blocked by cooldown; force_cooldown_bypass=True overrides."""
    params = _make_params(coins=["BTC"], concurrency_cap=3, signal_window_hours=3)

    # FP that failed exactly in the current hour
    failed_fp = replace(
        _make_fp(coin="BTC", state=FarbState.FAILED, id=2),
        closed_at=datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc),
    )

    # signal returns qualifying value
    signal_value = 0.50

    evaluator, farb_repo, signal_computer = _make_evaluator(
        mocker,
        params=params,
        non_terminal=[],
        by_coin_nonterminal={"BTC": []},
        by_coin_terminal={"BTC": [failed_fp]},
        signal_side_effect=[signal_value, signal_value],  # called twice (bypass test)
    )

    # First call: cooldown should block
    await evaluator.evaluate(now_ms=_NOW_MS, force_cooldown_bypass=False)
    farb_repo.create.assert_not_called()

    # Second call: bypass=True should allow the entry
    farb_repo.list_non_terminal.return_value = []
    await evaluator.evaluate(now_ms=_NOW_MS, force_cooldown_bypass=True)
    farb_repo.create.assert_called_once()


@pytest.mark.asyncio
async def test_top_k_selection_picks_strongest_signals(mocker):
    """3 coins with signals [0.5, 0.3, 0.7] and K=2 → creates FPs for 0.7 and 0.5."""
    params = _make_params(
        coins=["BTC", "ETH", "SOL"],
        concurrency_cap=2,
        budget_cap_usdc=99999.0,
    )

    # Signals in coin iteration order: BTC=0.5, ETH=0.3, SOL=0.7
    signals = {"BTC": 0.5, "ETH": 0.3, "SOL": 0.7}

    async def _compute(coin: str):
        return signals.get(coin)

    evaluator, farb_repo, signal_computer = _make_evaluator(
        mocker,
        params=params,
        non_terminal=[],
        by_coin_nonterminal={"BTC": [], "ETH": [], "SOL": []},
        by_coin_terminal={"BTC": [], "ETH": [], "SOL": []},
    )
    signal_computer.compute.side_effect = _compute

    await evaluator.evaluate(now_ms=_NOW_MS, force_cooldown_bypass=False)

    # Should have created 2 FarbPositions: SOL (0.7) and BTC (0.5)
    assert farb_repo.create.call_count == 2
    created_coins = {call.kwargs["coin"] for call in farb_repo.create.call_args_list}
    assert "SOL" in created_coins
    assert "BTC" in created_coins
    assert "ETH" not in created_coins


@pytest.mark.asyncio
async def test_budget_cap_blocks_all_entries(mocker):
    """Budget cap fully consumed → no new FPs created even with qualifying signals."""
    # footprint = budget/K = 1600/1 = 1600; 1 OPEN position consumes the entire budget
    params = _make_params(
        coins=["BTC", "ETH"],
        concurrency_cap=1,
        budget_cap_usdc=1600.0,
        position_size_usdc=1000.0,
        margin_buffer_factor=3.0,
    )
    # 1 PRE_BREAKEVEN position already consuming the entire budget
    existing_pre = _make_fp(coin="SOL", state=FarbState.PRE_BREAKEVEN, id=10)

    evaluator, farb_repo, signal_computer = _make_evaluator(
        mocker,
        params=params,
        non_terminal=[existing_pre],
        by_coin_nonterminal={"BTC": [], "ETH": []},
        by_coin_terminal={"BTC": [], "ETH": []},
        signal_side_effect=[0.50, 0.50],
    )

    await evaluator.evaluate(now_ms=_NOW_MS, force_cooldown_bypass=False)
    farb_repo.create.assert_not_called()
