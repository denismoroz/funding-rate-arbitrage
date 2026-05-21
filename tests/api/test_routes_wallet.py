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


# ── helpers ───────────────────────────────────────────────────────────────────


async def _seed_strategy(session_factory, *, name: str = "test_strat") -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(name=name, version="v1", params_json={}, status="running")
        s.add(strat)
        await s.flush()
        return strat.id


async def _seed_position_with_mark(
    session_factory,
    *,
    strategy_id: int,
    coin: str = "BTC",
    spot_units: float = 0.5,
    perp_units: float = -0.5,
    entry_spot_price: float = 40_000.0,
    entry_perp_price: float = 40_010.0,
    mark: float = 41_000.0,
) -> None:
    async with session_scope(session_factory) as s:
        exc = Exchange(name=f"HL_{coin}_{strategy_id}", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin=coin)
        s.add(mkt)
        await s.flush()
        pos = Position(
            strategy_id=strategy_id,
            market_id=mkt.id,
            mode=PositionMode.LIVE,
            status=PositionStatus.OPEN,
            opened_at=_utc(),
            spot_units=spot_units,
            perp_units=perp_units,
            entry_spot_price=entry_spot_price,
            entry_perp_price=entry_perp_price,
        )
        s.add(pos)
        await s.flush()
        s.add(Price(market_id=mkt.id, ts=_utc(), mark=mark))


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


# ── Test 2: Paper mode — synthesized from DB ───────────────────────────────────


async def test_paper_mode_synthesizes_from_db(session_factory, api_client_with_executor):
    """In paper mode (no executor), wallet is synthesized from DB positions + strategy.cash."""
    strategy_id = await _seed_strategy(session_factory, name="paper_strat")
    await _seed_position_with_mark(
        session_factory,
        strategy_id=strategy_id,
        coin="ETH",
        spot_units=1.0,
        perp_units=-1.0,
        entry_spot_price=3_000.0,
        entry_perp_price=3_010.0,
        mark=3_100.0,
    )

    # Mock strategy object (paper mode: no live executor)
    mock_strategy = MagicMock()
    mock_strategy.cash = 5_000.0

    client = await api_client_with_executor(None, strategy=mock_strategy, strategy_id=strategy_id)
    resp = await client.get(f"/api/equity/wallet?strategy_id={strategy_id}")

    assert resp.status_code == 200
    data = resp.json()

    # spot_balances: 1 ETH at $3100 = $3100
    assert len(data["spot_balances"]) == 1
    assert data["spot_balances"][0]["coin"] == "ETH"
    assert data["spot_balances"][0]["qty"] == pytest.approx(1.0)
    assert data["spot_balances"][0]["mark"] == pytest.approx(3_100.0)
    assert data["spot_balances"][0]["usd_value"] == pytest.approx(3_100.0)

    # perp_unrealized = abs(-1.0) * (3010 - 3100) = -90
    assert data["perp_unrealized_pnl"] == pytest.approx(-90.0)

    # usdc_spot = cash = 5000
    assert data["usdc_spot"] == pytest.approx(5_000.0)

    # perp_account_value = cash + perp_unrealized = 5000 + (-90) = 4910
    assert data["perp_account_value"] == pytest.approx(4_910.0)

    # total_usd = perp_account_value + spot_tokens_usd = 4910 + 3100 = 8010
    assert data["total_usd"] == pytest.approx(8_010.0)


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


# ── Test 4: Paper mode without engine running → 503 ───────────────────────────


async def test_paper_mode_without_engine_running_503(session_factory):
    """Paper mode with no engine running (app.state.strategy is None) → 503."""
    app = create_app(session_factory)
    # executor is None (default), strategy is None (default)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        resp = await client.get("/api/equity/wallet?strategy_id=999")

    assert resp.status_code == 503
    assert "Engine not running" in resp.json()["detail"]
