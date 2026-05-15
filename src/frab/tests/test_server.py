"""Tests for frab.server build_app and its lifespan helpers."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi.routing import APIWebSocketRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Exchange, Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.server import (
    DEFAULT_COINS,
    EXCHANGE_NAME,
    STRATEGY_NAME,
    STRATEGY_VERSION,
    _ensure_strategy,
    _resolve_exchange,
    build_app,
)


async def _factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    await init_db(engine)
    return engine, make_session_factory(engine)


async def test_ensure_strategy_creates_when_missing():
    engine, factory = await _factory()
    try:
        params = {"coins": list(DEFAULT_COINS), "concurrency_cap": 3}
        sid = await _ensure_strategy(factory, params)
        assert sid > 0

        async with session_scope(factory) as s:
            row = (await s.execute(
                select(Strategy).where(Strategy.id == sid)
            )).scalar_one()
            assert row.name == STRATEGY_NAME
            assert row.version == STRATEGY_VERSION
            assert row.params_json == params
    finally:
        await engine.dispose()


async def test_ensure_strategy_reuses_existing_row():
    engine, factory = await _factory()
    try:
        sid1 = await _ensure_strategy(factory, {"v": 1})
        sid2 = await _ensure_strategy(factory, {"v": 2})  # different params, same name/version
        assert sid1 == sid2

        async with session_scope(factory) as s:
            rows = (await s.execute(select(Strategy))).scalars().all()
            assert len(rows) == 1
    finally:
        await engine.dispose()


async def test_resolve_exchange_raises_when_unseeded():
    engine, factory = await _factory()
    try:
        try:
            await _resolve_exchange(factory)
        except RuntimeError as e:
            assert EXCHANGE_NAME in str(e)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        await engine.dispose()


async def test_resolve_exchange_returns_id_and_fees():
    engine, factory = await _factory()
    try:
        async with session_scope(factory) as s:
            s.add(Exchange(
                name=EXCHANGE_NAME,
                funding_interval_h=1,
                spot_taker_bps=7.0,
                perp_taker_bps=3.5,
            ))
            await s.flush()

        exc_id, spot_bps, perp_bps = await _resolve_exchange(factory)
        assert exc_id > 0
        assert spot_bps == 7.0
        assert perp_bps == 3.5
    finally:
        await engine.dispose()


def test_build_app_returns_fastapi_with_routes(tmp_path, monkeypatch):
    db_path = tmp_path / "frab.db"
    monkeypatch.setenv("FRAB_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("FRAB_DATA_DIR", str(tmp_path))

    app = build_app(coins=("BTC", "ETH"))

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/healthz" in paths
    assert "/api/strategies" in paths
    assert "/api/equity" in paths
    assert "/api/positions" in paths
    assert "/api/signals" in paths
    assert "/api/events" in paths
    assert any(p and p.startswith("/api/funding") for p in paths)

    assert any(
        isinstance(r, APIWebSocketRoute) and r.path == "/ws/live"
        for r in app.routes
    )

    assert app.state.event_bus is not None
    assert app.state.session_factory is not None
