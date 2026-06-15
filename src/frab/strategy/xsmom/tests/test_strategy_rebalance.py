"""Phase D orchestrator tests — scan-always, rebalance-when-due/active, manual_rebalance."""
from __future__ import annotations

import pytest

from frab.strategy.xsmom.params import XsmomParams
from frab.strategy.xsmom.strategy import XsmomStrategy

_NOW_MS = 1_704_067_200_000  # 2024-01-01 Monday


def _params() -> XsmomParams:
    return XsmomParams(budget_cap=1000.0, universe=("AAA", "BBB", "CCC"))


def _strategy_with_mocks(mocker, *, status: str, last_rebalance_ms):
    """Build an XsmomStrategy with internals replaced by AsyncMocks and a patched
    session_scope returning a row with the given status + params_json."""
    strategy = XsmomStrategy(
        strategy_id=1,
        exchange=mocker.AsyncMock(),
        xsmom_repo=mocker.AsyncMock(),
        session_factory=mocker.MagicMock(),
        params=_params(),
        settings=mocker.MagicMock(),
    )
    # Replace params-dependent internals with mocks.
    strategy._history_refresh = mocker.AsyncMock()
    strategy._scan_action = mocker.AsyncMock()
    strategy._scan_action.scan = mocker.AsyncMock(return_value={"scores": {"AAA": 1.0}})
    strategy._rebalance = mocker.AsyncMock()
    strategy._funding_accrual = mocker.AsyncMock()

    strat_row = mocker.MagicMock()
    strat_row.status = status
    strat_row.params_json = (
        {} if last_rebalance_ms is None else {"last_rebalance_ms": last_rebalance_ms}
    )

    mock_session = mocker.AsyncMock()
    mock_session.get = mocker.AsyncMock(return_value=strat_row)
    mock_session.__aenter__ = mocker.AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("frab.strategy.xsmom.strategy.session_scope", return_value=mock_session)

    return strategy


@pytest.mark.asyncio
async def test_on_hour_tick_scans_even_when_paused(mocker):
    strategy = _strategy_with_mocks(mocker, status="paused", last_rebalance_ms=None)

    await strategy.on_hour_tick(now_ms=_NOW_MS)

    strategy._history_refresh.refresh.assert_awaited_once()
    strategy._scan_action.scan.assert_awaited_once_with(now_ms=_NOW_MS)
    strategy._funding_accrual.accrue.assert_awaited_once()
    # Paused → NO reconcile even though never rebalanced.
    strategy._rebalance.reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_hour_tick_active_and_due_reconciles(mocker):
    # never rebalanced → due.
    strategy = _strategy_with_mocks(mocker, status="active", last_rebalance_ms=None)

    await strategy.on_hour_tick(now_ms=_NOW_MS)

    strategy._rebalance.reconcile.assert_awaited_once()
    # scores from the scan are forwarded to reconcile.
    assert strategy._rebalance.reconcile.await_args.kwargs["scores"] == {"AAA": 1.0}


@pytest.mark.asyncio
async def test_on_hour_tick_active_not_due_skips_reconcile(mocker):
    # rebalanced 1 day ago (< 7) → not due.
    strategy = _strategy_with_mocks(
        mocker, status="active", last_rebalance_ms=_NOW_MS - 86_400_000
    )

    await strategy.on_hour_tick(now_ms=_NOW_MS)

    strategy._rebalance.reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_rebalance_reconciles_even_when_paused(mocker):
    strategy = _strategy_with_mocks(mocker, status="paused", last_rebalance_ms=None)
    strategy._rebalance.reconcile = mocker.AsyncMock(return_value={"opened": [1]})

    result = await strategy.manual_rebalance(now_ms=_NOW_MS)

    strategy._history_refresh.refresh.assert_awaited_once()
    strategy._scan_action.scan.assert_awaited_once_with(now_ms=_NOW_MS)
    strategy._rebalance.reconcile.assert_awaited_once()
    assert result == {"opened": [1]}
