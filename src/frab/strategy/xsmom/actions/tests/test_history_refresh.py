"""Unit tests for XsmomHistoryRefresh — upsert all coins; one failure doesn't abort."""
from __future__ import annotations

import pytest

from frab.strategy.xsmom.actions.history_refresh import XsmomHistoryRefresh
from frab.strategy.xsmom.params import XsmomParams


def _params() -> XsmomParams:
    return XsmomParams(budget_cap=1000.0, universe=("AAA", "BBB"))


@pytest.mark.asyncio
async def test_refresh_upserts_all_coins(mocker):
    exchange = mocker.AsyncMock()
    exchange.get_daily_candles = mocker.AsyncMock(
        side_effect=lambda coin, days: [(1_000, 10.0), (2_000, 11.0)]
    )
    repo = mocker.AsyncMock()

    await XsmomHistoryRefresh(exchange=exchange, xsmom_repo=repo, params=_params()).refresh(days=2)

    repo.upsert_daily_prices.assert_awaited_once()
    rows = repo.upsert_daily_prices.await_args.args[0]
    coins = {r[0] for r in rows}
    assert coins == {"AAA", "BBB"}
    assert len(rows) == 4  # 2 candles x 2 coins


@pytest.mark.asyncio
async def test_refresh_one_coin_failure_does_not_abort(mocker):
    async def _candles(coin, days):
        if coin == "AAA":
            raise RuntimeError("HL fetch failed")
        return [(1_000, 10.0)]

    exchange = mocker.AsyncMock()
    exchange.get_daily_candles = mocker.AsyncMock(side_effect=_candles)
    repo = mocker.AsyncMock()

    await XsmomHistoryRefresh(exchange=exchange, xsmom_repo=repo, params=_params()).refresh()

    # BBB still upserted despite AAA failing.
    rows = repo.upsert_daily_prices.await_args.args[0]
    assert {r[0] for r in rows} == {"BBB"}


@pytest.mark.asyncio
async def test_refresh_no_rows_skips_upsert(mocker):
    exchange = mocker.AsyncMock()
    exchange.get_daily_candles = mocker.AsyncMock(return_value=[])
    repo = mocker.AsyncMock()

    await XsmomHistoryRefresh(exchange=exchange, xsmom_repo=repo, params=_params()).refresh()

    repo.upsert_daily_prices.assert_not_awaited()
