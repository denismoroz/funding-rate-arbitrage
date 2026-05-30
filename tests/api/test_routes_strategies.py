"""Tests for /api/strategies routes (TASK E)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from frab.api.app import create_app
from frab.db.models import Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.engine.loop import StrategyIdMismatch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(eng.sync_engine, "connect", _enable_fks)
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return make_session_factory(db_engine)


async def _seed_strategy(session_factory, *, name: str = "test_strat", params: dict | None = None) -> int:
    if params is None:
        params = {}
    async with session_scope(session_factory) as s:
        strat = Strategy(name=name, version="v1", params_json=params, status="active")
        s.add(strat)
        await s.flush()
        return strat.id


@pytest_asyncio.fixture
async def api_client(session_factory):
    app = create_app(session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# E.1 — get_strategy_or_404 dependency
# ---------------------------------------------------------------------------


class TestGetStrategy:
    async def test_get_strategy_200(self, session_factory, api_client):
        sid = await _seed_strategy(session_factory, name="strat_a")
        resp = await api_client.get(f"/api/strategies/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sid
        assert data["name"] == "strat_a"

    async def test_get_strategy_404(self, api_client):
        resp = await api_client.get("/api/strategies/9999")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Strategy not found"

    async def test_get_strategy_params_200(self, session_factory, api_client):
        sid = await _seed_strategy(session_factory, name="strat_b", params={"k": 3})
        resp = await api_client.get(f"/api/strategies/{sid}/params")
        assert resp.status_code == 200
        data = resp.json()
        assert data["params"]["k"] == 3

    async def test_get_strategy_params_404(self, api_client):
        resp = await api_client.get("/api/strategies/9999/params")
        assert resp.status_code == 404

    async def test_patch_strategy_params_404(self, api_client):
        resp = await api_client.patch(
            "/api/strategies/9999/params",
            json={"params": {}},
        )
        assert resp.status_code == 404

    async def test_pause_strategy_404(self, api_client):
        resp = await api_client.post("/api/strategies/9999/pause")
        assert resp.status_code == 404

    async def test_resume_strategy_404(self, api_client):
        resp = await api_client.post("/api/strategies/9999/resume")
        assert resp.status_code == 404

    async def test_pause_strategy_200(self, session_factory, api_client):
        sid = await _seed_strategy(session_factory, name="strat_pause")
        resp = await api_client.post(f"/api/strategies/{sid}/pause")
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused"

    async def test_resume_strategy_200(self, session_factory, api_client):
        sid = await _seed_strategy(session_factory, name="strat_resume")
        resp = await api_client.post(f"/api/strategies/{sid}/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"


# ---------------------------------------------------------------------------
# E.3 — force-tick endpoint
# ---------------------------------------------------------------------------


class TestForceHourTick:
    async def test_force_tick_no_engine_503(self, session_factory):
        """Returns 503 when engine_loop is not set on app.state."""
        app = create_app(session_factory)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/strategies/1/force-tick")
        assert resp.status_code == 503
        assert "Engine not configured" in resp.json()["detail"]

    async def test_force_tick_mismatch_400(self, session_factory, mocker):
        """Returns 400 when engine reports StrategyIdMismatch."""
        app = create_app(session_factory)
        mock_loop = mocker.AsyncMock()
        mock_loop.force_hour_tick.side_effect = StrategyIdMismatch(expected=1, got=2)
        app.state.engine_loop = mock_loop

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/strategies/2/force-tick")
        assert resp.status_code == 400
        assert "strategy_id mismatch" in resp.json()["detail"]

    async def test_force_tick_success_200(self, session_factory, mocker):
        """Returns 200 with expected payload on success."""
        app = create_app(session_factory)
        mock_loop = mocker.AsyncMock()
        mock_loop.force_hour_tick.return_value = None
        app.state.engine_loop = mock_loop

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/strategies/1/force-tick")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "ts_ms" in data
        assert data["message"] == "hour tick forced (params reloaded)"
        mock_loop.force_hour_tick.assert_called_once_with(strategy_id=1, now_ms=mocker.ANY)
