"""Unit tests for XsmomScanAction — ranking build + record_scan call.

Signal math is covered by the Phase B parity test; here compute_scores is patched
so we test the scan action's own logic (leg assignment, k, record_scan payload).
"""
from __future__ import annotations

import pytest

from frab.strategy.xsmom.actions.scan import XsmomScanAction
from frab.strategy.xsmom.params import XsmomParams

_NOW_MS = 1_704_067_200_000
_UNIVERSE = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")  # auto k = 6 // 3 = 2


def _params() -> XsmomParams:
    return XsmomParams(budget_cap=1000.0, universe=_UNIVERSE)


@pytest.mark.asyncio
async def test_scan_records_ranking_with_legs(mocker):
    repo = mocker.AsyncMock()
    repo.get_daily_closes = mocker.AsyncMock(return_value={})
    repo.record_scan = mocker.AsyncMock(return_value=42)

    # Patch compute_scores to a deterministic descending dict.
    scores = {"AAA": 5.0, "BBB": 4.0, "CCC": 3.0, "DDD": 2.0, "EEE": 1.0, "FFF": 0.0}
    mocker.patch("frab.strategy.xsmom.actions.scan.compute_scores", return_value=scores)

    action = XsmomScanAction(strategy_id=1, xsmom_repo=repo, params=_params())
    result = await action.scan(now_ms=_NOW_MS)

    assert result["k"] == 2
    assert result["scan_id"] == 42
    assert result["n_long"] == 2 and result["n_short"] == 2

    # record_scan called with ranking where top-2 = long, bottom-2 = short, mid = None
    repo.record_scan.assert_awaited_once()
    kwargs = repo.record_scan.await_args.kwargs
    ranking = kwargs["ranking"]
    legs = {r["coin"]: r["leg"] for r in ranking}
    assert legs["AAA"] == "long" and legs["BBB"] == "long"
    assert legs["EEE"] == "short" and legs["FFF"] == "short"
    assert legs["CCC"] is None and legs["DDD"] is None
    assert kwargs["ts_ms"] == _NOW_MS


@pytest.mark.asyncio
async def test_scan_returns_scores_for_reuse(mocker):
    repo = mocker.AsyncMock()
    repo.get_daily_closes = mocker.AsyncMock(return_value={})
    repo.record_scan = mocker.AsyncMock(return_value=1)
    scores = {"AAA": 2.0, "FFF": -2.0}
    mocker.patch("frab.strategy.xsmom.actions.scan.compute_scores", return_value=scores)

    action = XsmomScanAction(strategy_id=1, xsmom_repo=repo, params=_params())
    result = await action.scan(now_ms=_NOW_MS)

    assert result["scores"] == scores  # reusable by rebalance, no recompute
