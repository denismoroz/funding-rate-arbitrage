"""Tests for GET /api/equity/wallet."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from frab.api.app import create_app
from frab.db.models import Exchange, Market, Position, PositionMode, PositionStatus, Price, Strategy
from frab.db.session import session_scope


def _utc() -> datetime:
    return datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_strategy(session_factory, *, name: str = "test_strat") -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(name=name, version="v1", params_json={}, status="running")
        s.add(strat)
        await s.flush()
        return strat.id


# ── Test 1: Live mode — executor.fetch_wallet_state returns fixed dict → 200 ──


async def test_live_mode_returns_executor_wallet(session_factory, api_client_with_executor):
    """In live mode, the route delegates to executor.fetch_wallet_state and returns it."""
    executor = MagicMock()
    executor.fetch_wallet_state = AsyncMock(return_value={
        "perp_account_value": 1000.0,
        "perp_unrealized_pnl": -50.0,
        "spot_balances": [
            {"coin": "BTC", "qty": 0.001, "mark": 95_000.0, "usd_value": 95.0}
        ],
        "usdc_spot": 200.0,
        "total_usd": 1295.0,
    })

    strategy_id = await _seed_strategy(session_factory, name="live_strat")

    client = await api_client_with_executor(executor, strategy_id=strategy_id)
    resp = await client.get(f"/api/equity/wallet?strategy_id={strategy_id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["perp_account_value"] == pytest.approx(1000.0)
    assert data["perp_unrealized_pnl"] == pytest.approx(-50.0)
    assert data["usdc_spot"] == pytest.approx(200.0)
    assert data["total_usd"] == pytest.approx(1295.0)
    assert len(data["spot_balances"]) == 1
    assert data["spot_balances"][0]["coin"] == "BTC"
    assert data["spot_balances"][0]["qty"] == pytest.approx(0.001)


# ── Test 2: No executor → 503 ─────────────────────────────────────────────────


async def test_no_executor_returns_503(session_factory, api_client_with_executor):
    """When no executor is wired, the endpoint returns 503."""
    strategy_id = await _seed_strategy(session_factory, name="noexec_strat")
    client = await api_client_with_executor(None, strategy_id=strategy_id)
    resp = await client.get(f"/api/equity/wallet?strategy_id={strategy_id}")
    assert resp.status_code == 503
    assert "Engine not configured" in resp.json()["detail"]


# ── Test 3: Executor raises → 503 ─────────────────────────────────────────────


async def test_live_mode_executor_raises_503(session_factory, api_client_with_executor):
    """When executor.fetch_wallet_state raises, the endpoint returns 503."""
    executor = MagicMock()
    executor.fetch_wallet_state = AsyncMock(side_effect=RuntimeError("HL API unreachable"))

    strategy_id = await _seed_strategy(session_factory, name="err_strat")

    client = await api_client_with_executor(executor, strategy_id=strategy_id)
    resp = await client.get(f"/api/equity/wallet?strategy_id={strategy_id}")

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "HL API unreachable" in detail


# ── Test 4: No executor (default app) → 503 ───────────────────────────────────


async def test_default_app_no_executor_503(session_factory):
    """Default app without executor wired returns 503."""
    app = create_app(session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        resp = await client.get("/api/equity/wallet?strategy_id=999")

    assert resp.status_code == 503
    assert "Engine not configured" in resp.json()["detail"]
