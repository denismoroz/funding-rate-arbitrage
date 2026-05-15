"""Tests for strategy params hot-swap API routes."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from frab.api.app import create_app
from frab.db.models import Strategy
from frab.db.session import session_scope
from frab.events.bus import EventBus
from frab.strategies.strategy_a import StrategyA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PARAMS_JSON = {
    "coins": ["BTC", "ETH", "SOL"],
    "entry_threshold": 0.30,
    "exit_threshold": -0.15,
    "min_hold_hours": 120,
    "signal_window_hours": 12,
    "concurrency_cap": 3,
    "position_size_usdc": 1000.0,
}

_VALID_DEPLOY_BODY = {
    "entry_threshold": 0.50,
    "exit_threshold": -0.10,
    "min_hold_hours": 24,
    "concurrency_cap": 5,
    "position_size_usdc": 500.0,
}


async def _seed_strategy(session_factory, *, params_json: dict | None = None) -> int:
    pj = params_json if params_json is not None else _PARAMS_JSON.copy()
    async with session_scope(session_factory) as s:
        row = Strategy(
            name="strategy_a",
            version="v1",
            params_json=pj,
            status="idle",
        )
        s.add(row)
        await s.flush()
        return row.id


def _make_client(session_factory, *, strategy: object = None, strategy_id: int | None = None, event_bus: object = None, engine: object = None):
    """Build a test AsyncClient with optional app.state overrides."""
    app = create_app(session_factory, event_bus=event_bus)
    if strategy is not None:
        app.state.strategy = strategy
    if strategy_id is not None:
        app.state.strategy_id = strategy_id
    if engine is not None:
        app.state.engine = engine
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True)


# ---------------------------------------------------------------------------
# GET /{strategy_id}/params
# ---------------------------------------------------------------------------


async def test_get_strategy_params_returns_persisted_params(session_factory):
    sid = await _seed_strategy(session_factory)

    async with _make_client(session_factory) as client:
        resp = await client.get(f"/api/strategies/{sid}/params")

    assert resp.status_code == 200
    data = resp.json()
    assert data["coins"] == ["BTC", "ETH", "SOL"]
    assert data["entry_threshold"] == pytest.approx(0.30)
    assert data["exit_threshold"] == pytest.approx(-0.15)
    assert data["min_hold_hours"] == 120
    assert data["signal_window_hours"] == 12
    assert data["concurrency_cap"] == 3
    assert data["position_size_usdc"] == pytest.approx(1000.0)


async def test_get_strategy_params_404(session_factory):
    async with _make_client(session_factory) as client:
        resp = await client.get("/api/strategies/99999/params")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /{strategy_id}/deploy — happy path
# ---------------------------------------------------------------------------


async def test_deploy_strategy_params_updates_db_and_calls_strategy(session_factory):
    sid = await _seed_strategy(session_factory)

    mock_strategy = MagicMock(spec=StrategyA)

    async with _make_client(session_factory, strategy=mock_strategy, strategy_id=sid) as client:
        resp = await client.post(f"/api/strategies/{sid}/deploy", json=_VALID_DEPLOY_BODY)

    assert resp.status_code == 200
    data = resp.json()

    # Merged: cold fields from DB + hot fields from body
    assert data["coins"] == ["BTC", "ETH", "SOL"]
    assert data["signal_window_hours"] == 12
    assert data["entry_threshold"] == pytest.approx(0.50)
    assert data["exit_threshold"] == pytest.approx(-0.10)
    assert data["min_hold_hours"] == 24
    assert data["concurrency_cap"] == 5
    assert data["position_size_usdc"] == pytest.approx(500.0)

    # update_hot_params called once with exact hot kwargs
    mock_strategy.update_hot_params.assert_called_once_with(
        entry_threshold=0.50,
        exit_threshold=-0.10,
        min_hold_hours=24,
        concurrency_cap=5,
        position_size_usdc=500.0,
    )

    # DB row updated
    async with session_scope(session_factory) as s:
        result = await s.execute(select(Strategy).where(Strategy.id == sid))
        row = result.scalar_one()
    assert row.params_json["entry_threshold"] == pytest.approx(0.50)
    assert row.params_json["exit_threshold"] == pytest.approx(-0.10)
    assert row.params_json["min_hold_hours"] == 24
    assert row.params_json["concurrency_cap"] == 5
    assert row.params_json["position_size_usdc"] == pytest.approx(500.0)
    # Cold fields still there
    assert row.params_json["coins"] == ["BTC", "ETH", "SOL"]
    assert row.params_json["signal_window_hours"] == 12


async def test_deploy_strategy_params_emits_event(session_factory):
    sid = await _seed_strategy(session_factory)

    mock_strategy = MagicMock(spec=StrategyA)
    mock_bus = MagicMock(spec=EventBus)
    mock_bus.publish = AsyncMock()

    async with _make_client(session_factory, strategy=mock_strategy, strategy_id=sid, event_bus=mock_bus) as client:
        resp = await client.post(f"/api/strategies/{sid}/deploy", json=_VALID_DEPLOY_BODY)

    assert resp.status_code == 200
    mock_bus.publish.assert_awaited_once()
    published_event = mock_bus.publish.call_args[0][0]
    assert published_event.kind == "strategy.params_updated"
    assert published_event.level == "INFO"
    assert published_event.source == "api"
    assert str(sid) in published_event.message


# ---------------------------------------------------------------------------
# POST /{strategy_id}/deploy — validation failures (422)
# ---------------------------------------------------------------------------


async def test_deploy_strategy_params_validation_exit_ge_entry(session_factory):
    sid = await _seed_strategy(session_factory)
    mock_strategy = MagicMock(spec=StrategyA)

    body = {**_VALID_DEPLOY_BODY, "exit_threshold": 0.50, "entry_threshold": 0.50}
    async with _make_client(session_factory, strategy=mock_strategy, strategy_id=sid) as client:
        resp = await client.post(f"/api/strategies/{sid}/deploy", json=body)

    assert resp.status_code == 422


async def test_deploy_strategy_params_validation_negative_size(session_factory):
    sid = await _seed_strategy(session_factory)
    mock_strategy = MagicMock(spec=StrategyA)

    body = {**_VALID_DEPLOY_BODY, "position_size_usdc": -1}
    async with _make_client(session_factory, strategy=mock_strategy, strategy_id=sid) as client:
        resp = await client.post(f"/api/strategies/{sid}/deploy", json=body)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /{strategy_id}/deploy — engine not running (503)
# ---------------------------------------------------------------------------


async def test_deploy_strategy_params_503_when_engine_not_running(session_factory):
    sid = await _seed_strategy(session_factory)

    # No strategy attached to app.state
    async with _make_client(session_factory) as client:
        resp = await client.post(f"/api/strategies/{sid}/deploy", json=_VALID_DEPLOY_BODY)

    assert resp.status_code == 503
    assert "not running" in resp.json()["detail"].lower()


async def test_deploy_strategy_params_503_when_wrong_strategy_id(session_factory):
    sid = await _seed_strategy(session_factory)

    mock_strategy = MagicMock(spec=StrategyA)
    # Attach strategy with a *different* strategy_id
    async with _make_client(session_factory, strategy=mock_strategy, strategy_id=sid + 1000) as client:
        resp = await client.post(f"/api/strategies/{sid}/deploy", json=_VALID_DEPLOY_BODY)

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /{strategy_id}/deploy — 404
# ---------------------------------------------------------------------------


async def test_deploy_strategy_params_404(session_factory):
    mock_strategy = MagicMock(spec=StrategyA)

    async with _make_client(session_factory, strategy=mock_strategy, strategy_id=99999) as client:
        resp = await client.post("/api/strategies/99999/deploy", json=_VALID_DEPLOY_BODY)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /{strategy_id}/deploy — no event bus (bus is None)
# ---------------------------------------------------------------------------


async def test_deploy_strategy_params_no_event_bus_does_not_raise(session_factory):
    sid = await _seed_strategy(session_factory)
    mock_strategy = MagicMock(spec=StrategyA)

    # event_bus=None (default in create_app)
    async with _make_client(session_factory, strategy=mock_strategy, strategy_id=sid) as client:
        resp = await client.post(f"/api/strategies/{sid}/deploy", json=_VALID_DEPLOY_BODY)

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Direct function-call tests (bypass ASGI for coverage tracking)
# ---------------------------------------------------------------------------


async def test_direct_get_strategy_params_found(session_factory):
    from frab.api.routes.strategies import get_strategy_params

    sid = await _seed_strategy(session_factory)

    async with session_scope(session_factory) as session:
        result = await get_strategy_params(strategy_id=sid, session=session)

    assert result.coins == ["BTC", "ETH", "SOL"]
    assert result.entry_threshold == pytest.approx(0.30)
    assert result.concurrency_cap == 3


async def test_direct_get_strategy_params_not_found(session_factory):
    from frab.api.routes.strategies import get_strategy_params

    async with session_scope(session_factory) as session:
        with pytest.raises(HTTPException) as exc_info:
            await get_strategy_params(strategy_id=99999, session=session)
    assert exc_info.value.status_code == 404


async def test_direct_deploy_strategy_params_success(session_factory):
    from unittest.mock import MagicMock

    from fastapi import Request

    from frab.api.routes.strategies import deploy_strategy_params
    from frab.api.schemas import StrategyParamsIn

    sid = await _seed_strategy(session_factory)

    mock_strategy = MagicMock(spec=StrategyA)

    # Build a minimal Request-like object with app.state
    mock_app_state = MagicMock()
    mock_app_state.strategy = mock_strategy
    mock_app_state.strategy_id = sid
    mock_app_state.event_bus = None
    mock_request = MagicMock(spec=Request)
    mock_request.app.state = mock_app_state

    params_in = StrategyParamsIn(
        entry_threshold=0.40,
        exit_threshold=-0.20,
        min_hold_hours=48,
        concurrency_cap=2,
        position_size_usdc=750.0,
    )

    async with session_scope(session_factory) as session:
        result = await deploy_strategy_params(
            strategy_id=sid,
            params_in=params_in,
            request=mock_request,
            session=session,
        )

    assert result.entry_threshold == pytest.approx(0.40)
    assert result.exit_threshold == pytest.approx(-0.20)
    assert result.min_hold_hours == 48
    assert result.concurrency_cap == 2
    assert result.position_size_usdc == pytest.approx(750.0)
    # Cold fields preserved
    assert result.coins == ["BTC", "ETH", "SOL"]
    assert result.signal_window_hours == 12

    mock_strategy.update_hot_params.assert_called_once_with(
        entry_threshold=0.40,
        exit_threshold=-0.20,
        min_hold_hours=48,
        concurrency_cap=2,
        position_size_usdc=750.0,
    )


async def test_direct_deploy_strategy_params_404(session_factory):
    from unittest.mock import MagicMock

    from fastapi import Request

    from frab.api.routes.strategies import deploy_strategy_params
    from frab.api.schemas import StrategyParamsIn

    mock_strategy = MagicMock(spec=StrategyA)
    mock_app_state = MagicMock()
    mock_app_state.strategy = mock_strategy
    mock_app_state.strategy_id = 99999
    mock_app_state.event_bus = None
    mock_request = MagicMock(spec=Request)
    mock_request.app.state = mock_app_state

    params_in = StrategyParamsIn(
        entry_threshold=0.40,
        exit_threshold=-0.20,
        min_hold_hours=48,
        concurrency_cap=2,
        position_size_usdc=750.0,
    )

    async with session_scope(session_factory) as session:
        with pytest.raises(HTTPException) as exc_info:
            await deploy_strategy_params(
                strategy_id=99999,
                params_in=params_in,
                request=mock_request,
                session=session,
            )
    assert exc_info.value.status_code == 404


async def test_direct_deploy_strategy_params_503_no_strategy(session_factory):
    from unittest.mock import MagicMock

    from fastapi import Request

    from frab.api.routes.strategies import deploy_strategy_params
    from frab.api.schemas import StrategyParamsIn

    sid = await _seed_strategy(session_factory)

    mock_app_state = MagicMock()
    mock_app_state.strategy = None
    mock_app_state.strategy_id = None
    mock_request = MagicMock(spec=Request)
    mock_request.app.state = mock_app_state

    params_in = StrategyParamsIn(
        entry_threshold=0.40,
        exit_threshold=-0.20,
        min_hold_hours=48,
        concurrency_cap=2,
        position_size_usdc=750.0,
    )

    async with session_scope(session_factory) as session:
        with pytest.raises(HTTPException) as exc_info:
            await deploy_strategy_params(
                strategy_id=sid,
                params_in=params_in,
                request=mock_request,
                session=session,
            )
    assert exc_info.value.status_code == 503


async def test_direct_deploy_strategy_params_with_event_bus(session_factory):
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import Request

    from frab.api.routes.strategies import deploy_strategy_params
    from frab.api.schemas import StrategyParamsIn

    sid = await _seed_strategy(session_factory)

    mock_strategy = MagicMock(spec=StrategyA)
    mock_bus = MagicMock(spec=EventBus)
    mock_bus.publish = AsyncMock()

    mock_app_state = MagicMock()
    mock_app_state.strategy = mock_strategy
    mock_app_state.strategy_id = sid
    mock_app_state.event_bus = mock_bus
    mock_request = MagicMock(spec=Request)
    mock_request.app.state = mock_app_state

    params_in = StrategyParamsIn(
        entry_threshold=0.40,
        exit_threshold=-0.20,
        min_hold_hours=48,
        concurrency_cap=2,
        position_size_usdc=750.0,
    )

    async with session_scope(session_factory) as session:
        await deploy_strategy_params(
            strategy_id=sid,
            params_in=params_in,
            request=mock_request,
            session=session,
        )

    mock_bus.publish.assert_awaited_once()
    event = mock_bus.publish.call_args[0][0]
    assert event.kind == "strategy.params_updated"


# ---------------------------------------------------------------------------
# POST /{strategy_id}/force-tick
# ---------------------------------------------------------------------------


async def test_force_tick_calls_engine_and_emits_event(session_factory):
    from frab.engine.loop import Engine

    sid = await _seed_strategy(session_factory)
    mock_engine = MagicMock(spec=Engine)
    mock_bus = MagicMock(spec=EventBus)
    mock_bus.publish = AsyncMock()

    async with _make_client(
        session_factory,
        strategy_id=sid,
        engine=mock_engine,
        event_bus=mock_bus,
    ) as client:
        resp = await client.post(f"/api/strategies/{sid}/force-tick")

    assert resp.status_code == 200
    assert resp.json()["status"] == "scheduled"
    mock_engine.force_hour_tick.assert_called_once_with()
    mock_bus.publish.assert_awaited_once()
    assert mock_bus.publish.call_args[0][0].kind == "engine.force_tick_requested"


async def test_force_tick_503_when_engine_not_running(session_factory):
    sid = await _seed_strategy(session_factory)

    async with _make_client(session_factory, strategy_id=sid) as client:
        resp = await client.post(f"/api/strategies/{sid}/force-tick")

    assert resp.status_code == 503


async def test_force_tick_404_when_strategy_missing(session_factory):
    async with _make_client(session_factory) as client:
        resp = await client.post("/api/strategies/99999/force-tick")

    assert resp.status_code == 404
