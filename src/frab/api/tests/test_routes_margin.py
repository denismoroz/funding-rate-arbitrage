"""Tests for GET /api/equity/margin endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from frab.api.app import create_app
from frab.engine.margin_manager import (
    AccountAssessment,
    FpAssessment,
    MarginManager,
    MarginStatus,
)


def _canned_assessment() -> AccountAssessment:
    return AccountAssessment(
        account_ratio=2.5,
        account_equity_usdc=1000.0,
        total_maintenance_usdc=400.0,
        account_status=MarginStatus.HEALTHY,
        per_fp=[
            FpAssessment(
                farb_position_id=1,
                coin="BTC",
                virtual_equity=50.0,
                virtual_maintenance=20.0,
                virtual_ratio=2.5,
                status=MarginStatus.HEALTHY,
            ),
            FpAssessment(
                farb_position_id=2,
                coin="ETH",
                virtual_equity=30.0,
                virtual_maintenance=15.0,
                virtual_ratio=2.0,
                status=MarginStatus.WARNING,
            ),
        ],
        weakest_fp_id=None,
    )


async def test_returns_503_when_watchdog_missing(session_factory, farb_repo):
    app = create_app(session_factory, farb_repo=farb_repo)
    # app.state.margin_watchdog is unset → getattr returns None → 503
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/equity/margin")
    assert resp.status_code == 503
    assert "MarginWatchdog not configured" in resp.json()["detail"]


async def test_returns_full_shape(session_factory, farb_repo, mocker):
    app = create_app(session_factory, farb_repo=farb_repo)

    mock_watchdog = mocker.MagicMock()
    mock_watchdog.dry_assess = mocker.AsyncMock(return_value=_canned_assessment())
    mock_watchdog._mgr = MarginManager(
        top_up_trigger=2.0,
        forced_close_trigger=1.5,
        healthy_ratio=2.0,
    )
    app.state.margin_watchdog = mock_watchdog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/equity/margin")

    assert resp.status_code == 200
    data = resp.json()

    assert set(data.keys()) == {"ts_ms", "account", "thresholds", "per_fp", "weakest_fp_id"}
    assert isinstance(data["ts_ms"], int)
    assert data["ts_ms"] > 0

    account = data["account"]
    assert account["ratio"] == 2.5
    assert account["status"] == "healthy"
    assert account["equity_usdc"] == 1000.0
    assert account["total_maintenance_usdc"] == 400.0


async def test_thresholds_exposed(session_factory, farb_repo, mocker):
    app = create_app(session_factory, farb_repo=farb_repo)

    manager = MarginManager(
        top_up_trigger=2.0,
        forced_close_trigger=1.5,
        healthy_ratio=2.0,
    )
    mock_watchdog = mocker.MagicMock()
    mock_watchdog.dry_assess = mocker.AsyncMock(return_value=_canned_assessment())
    mock_watchdog._mgr = manager
    app.state.margin_watchdog = mock_watchdog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/equity/margin")

    assert resp.status_code == 200
    assert resp.json()["thresholds"] == {
        "healthy": 2.0,
        "forced_close": 1.5,
        "liquidation": 1.0,
    }


async def test_per_fp_array_correct(session_factory, farb_repo, mocker):
    app = create_app(session_factory, farb_repo=farb_repo)

    mock_watchdog = mocker.MagicMock()
    mock_watchdog.dry_assess = mocker.AsyncMock(return_value=_canned_assessment())
    mock_watchdog._mgr = MarginManager(
        top_up_trigger=2.0,
        forced_close_trigger=1.5,
        healthy_ratio=2.0,
    )
    app.state.margin_watchdog = mock_watchdog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/equity/margin")

    assert resp.status_code == 200
    per_fp = resp.json()["per_fp"]
    assert len(per_fp) == 2

    btc = per_fp[0]
    assert btc["farb_position_id"] == 1
    assert btc["coin"] == "BTC"
    assert btc["virtual_ratio"] == 2.5
    assert btc["status"] == "healthy"
    assert btc["virtual_equity_usdc"] == 50.0
    assert btc["virtual_maintenance_usdc"] == 20.0

    eth = per_fp[1]
    assert eth["farb_position_id"] == 2
    assert eth["coin"] == "ETH"
    assert eth["virtual_ratio"] == 2.0
    assert eth["status"] == "warning"
    assert eth["virtual_equity_usdc"] == 30.0
    assert eth["virtual_maintenance_usdc"] == 15.0
