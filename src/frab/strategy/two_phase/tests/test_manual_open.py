"""Unit tests for TwoPhaseStrategy.manual_open."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.strategy import (
    TwoPhaseStrategy,
    ManualOpenCoinNotInUniverse,
    ManualOpenAlreadyExists,
    ManualOpenConcurrencyCapReached,
    ManualOpenBudgetCapReached,
    ManualOpenSignalUnavailable,
)

_NOW_MS = 1_700_000_000_000


def _make_params(**overrides) -> TwoPhaseParams:
    defaults = dict(
        coins=["BTC", "ETH", "SOL"],
        concurrency_cap=3,
        budget_cap_usdc=3000.0,
        position_size_usdc=1000.0,
        margin_buffer_factor=3.0,
    )
    defaults.update(overrides)
    return TwoPhaseParams(**defaults)


def _make_fp(coin: str = "BTC", state: FarbState = FarbState.PRE_BREAKEVEN) -> FarbPosition:
    return FarbPosition(
        id=1,
        strategy_id=1,
        coin=coin,
        state=state,
        state_data={},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_strategy(mocker, *, params: TwoPhaseParams | None = None) -> TwoPhaseStrategy:
    p = params or _make_params()
    strategy = mocker.MagicMock(spec=TwoPhaseStrategy)
    strategy.strategy_id = 1
    strategy.params = p
    strategy.farb_repo = mocker.AsyncMock()
    strategy._signal_computer = mocker.AsyncMock()
    strategy._bus = None
    strategy.manual_open = TwoPhaseStrategy.manual_open.__get__(strategy)
    return strategy


@pytest.mark.asyncio
async def test_manual_open_rejects_coin_not_in_universe(mocker):
    strategy = _make_strategy(mocker)

    with pytest.raises(ManualOpenCoinNotInUniverse):
        await strategy.manual_open(coin="DOGE", now_ms=_NOW_MS)

    strategy.farb_repo.list_by_coin.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_open_rejects_existing_non_terminal_fp(mocker):
    strategy = _make_strategy(mocker)
    # list_by_coin returns an existing non-terminal position → AlreadyExists
    strategy.farb_repo.list_by_coin.return_value = [_make_fp("BTC")]

    with pytest.raises(ManualOpenAlreadyExists):
        await strategy.manual_open(coin="BTC", now_ms=_NOW_MS)


@pytest.mark.asyncio
async def test_manual_open_rejects_existing_pre_breakeven_fp(mocker):
    """PRE_BREAKEVEN position for the coin → AlreadyExists (list_by_coin non-terminal)."""
    strategy = _make_strategy(mocker)
    strategy.farb_repo.list_by_coin.return_value = [_make_fp("BTC", FarbState.PRE_BREAKEVEN)]

    with pytest.raises(ManualOpenAlreadyExists):
        await strategy.manual_open(coin="BTC", now_ms=_NOW_MS)


@pytest.mark.asyncio
async def test_manual_open_rejects_concurrency_cap_reached(mocker):
    params = _make_params(concurrency_cap=2)
    strategy = _make_strategy(mocker, params=params)
    strategy.farb_repo.list_by_coin.return_value = []
    # list_non_terminal returns 2 positions → cap reached
    strategy.farb_repo.list_non_terminal.return_value = [
        _make_fp("ETH", FarbState.PRE_BREAKEVEN),
        _make_fp("SOL", FarbState.PRE_BREAKEVEN),
    ]

    with pytest.raises(ManualOpenConcurrencyCapReached):
        await strategy.manual_open(coin="BTC", now_ms=_NOW_MS)


@pytest.mark.asyncio
async def test_manual_open_rejects_budget_cap_reached(mocker):
    # concurrency_cap=5 leaves room for more; two non-terminal positions.
    # With footprint=300, committed=600; 600+300=900 > budget_cap_usdc=500 → reject.
    params = _make_params(concurrency_cap=5, budget_cap_usdc=500.0)
    strategy = _make_strategy(mocker, params=params)
    strategy.farb_repo.list_by_coin.return_value = []
    strategy.farb_repo.list_non_terminal.return_value = [
        _make_fp("ETH", FarbState.PRE_BREAKEVEN),
        _make_fp("SOL", FarbState.PRE_BREAKEVEN),
    ]

    from frab.strategy.two_phase.params import TwoPhaseParams as _Params
    mocker.patch.object(_Params, "compute_footprint", return_value=300.0)

    with pytest.raises(ManualOpenBudgetCapReached):
        await strategy.manual_open(coin="BTC", now_ms=_NOW_MS)


@pytest.mark.asyncio
async def test_manual_open_rejects_signal_unavailable(mocker):
    strategy = _make_strategy(mocker)
    strategy.farb_repo.list_by_coin.return_value = []
    strategy.farb_repo.list_non_terminal.return_value = []
    strategy._signal_computer.compute.return_value = None

    with pytest.raises(ManualOpenSignalUnavailable):
        await strategy.manual_open(coin="BTC", now_ms=_NOW_MS)

    strategy.farb_repo.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_open_success(mocker):
    strategy = _make_strategy(mocker)
    strategy.farb_repo.list_by_coin.return_value = []
    strategy.farb_repo.list_non_terminal.return_value = []
    strategy._signal_computer.compute.return_value = 0.07

    created_fp = _make_fp("BTC", FarbState.CHECK_MARGIN)
    strategy.farb_repo.create.return_value = created_fp

    bus = mocker.AsyncMock()
    strategy._bus = bus

    result = await strategy.manual_open(coin="BTC", now_ms=_NOW_MS)

    assert result is created_fp

    strategy.farb_repo.create.assert_awaited_once_with(
        strategy_id=1,
        coin="BTC",
        initial_state=FarbState.CHECK_MARGIN,
        state_data={
            "target_signal_apr": 0.07,
            "entry_ts_ms": _NOW_MS,
            "manual_open": True,
        },
    )

    bus.publish.assert_awaited_once()
    published = bus.publish.await_args.args[0]
    assert published.kind == "farb.manual_open"
    assert published.level == "INFO"
