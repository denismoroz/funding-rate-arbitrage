"""Tests for /api/farb-positions/manual-open route."""
from __future__ import annotations

import pytest

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.strategy import (
    ManualOpenCoinNotInUniverse,
    ManualOpenAlreadyExists,
    ManualOpenConcurrencyCapReached,
    ManualOpenBudgetCapReached,
    ManualOpenSignalUnavailable,
)
from datetime import datetime, timezone


def _make_fp() -> FarbPosition:
    return FarbPosition(
        id=42,
        strategy_id=1,
        coin="BTC",
        state=FarbState.CHECK_MARGIN,
        state_data={"target_signal_apr": 0.07, "entry_ts_ms": 1700000000000, "manual_open": True},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


@pytest.mark.asyncio
async def test_manual_open_success(api_client_with_executor, mocker):
    mock_strategy = mocker.MagicMock()
    mock_strategy.manual_open = mocker.AsyncMock(return_value=_make_fp())

    client = await api_client_with_executor(executor=mocker.MagicMock(), strategy=mock_strategy)
    resp = await client.post(
        "/api/farb-positions/manual-open",
        json={"coin": "BTC"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 42
    assert data["coin"] == "BTC"
    assert data["state"] == "check_margin"
    assert "ts_ms" in data


@pytest.mark.asyncio
async def test_manual_open_503_when_no_strategy(api_client_with_executor, mocker):
    client = await api_client_with_executor(executor=mocker.MagicMock(), strategy=None)
    resp = await client.post(
        "/api/farb-positions/manual-open",
        json={"coin": "BTC"},
    )

    assert resp.status_code == 503
    assert "Strategy not configured" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_manual_open_400_coin_not_in_universe(api_client_with_executor, mocker):
    mock_strategy = mocker.MagicMock()
    mock_strategy.manual_open = mocker.AsyncMock(side_effect=ManualOpenCoinNotInUniverse("DOGE"))

    client = await api_client_with_executor(executor=mocker.MagicMock(), strategy=mock_strategy)
    resp = await client.post(
        "/api/farb-positions/manual-open",
        json={"coin": "DOGE"},
    )

    assert resp.status_code == 400
    assert "not in strategy universe" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_manual_open_409_already_exists(api_client_with_executor, mocker):
    mock_strategy = mocker.MagicMock()
    mock_strategy.manual_open = mocker.AsyncMock(side_effect=ManualOpenAlreadyExists("BTC"))

    client = await api_client_with_executor(executor=mocker.MagicMock(), strategy=mock_strategy)
    resp = await client.post(
        "/api/farb-positions/manual-open",
        json={"coin": "BTC"},
    )

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_manual_open_409_concurrency_cap(api_client_with_executor, mocker):
    mock_strategy = mocker.MagicMock()
    mock_strategy.manual_open = mocker.AsyncMock(
        side_effect=ManualOpenConcurrencyCapReached("3/3")
    )

    client = await api_client_with_executor(executor=mocker.MagicMock(), strategy=mock_strategy)
    resp = await client.post(
        "/api/farb-positions/manual-open",
        json={"coin": "BTC"},
    )

    assert resp.status_code == 409
    assert "concurrency cap reached" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_manual_open_409_budget_cap(api_client_with_executor, mocker):
    mock_strategy = mocker.MagicMock()
    mock_strategy.manual_open = mocker.AsyncMock(
        side_effect=ManualOpenBudgetCapReached("committed=3000.00 + footprint=1000.00 > cap=3000.00")
    )

    client = await api_client_with_executor(executor=mocker.MagicMock(), strategy=mock_strategy)
    resp = await client.post(
        "/api/farb-positions/manual-open",
        json={"coin": "BTC"},
    )

    assert resp.status_code == 409
    assert "budget cap reached" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_manual_open_422_signal_unavailable(api_client_with_executor, mocker):
    mock_strategy = mocker.MagicMock()
    mock_strategy.manual_open = mocker.AsyncMock(
        side_effect=ManualOpenSignalUnavailable("BTC")
    )

    client = await api_client_with_executor(executor=mocker.MagicMock(), strategy=mock_strategy)
    resp = await client.post(
        "/api/farb-positions/manual-open",
        json={"coin": "BTC"},
    )

    assert resp.status_code == 422
    assert "signal unavailable" in resp.json()["detail"]
