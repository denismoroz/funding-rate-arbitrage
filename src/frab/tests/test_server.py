"""Tests for frab.server build_app and its lifespan helpers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIWebSocketRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Exchange, FundingRate, Market, Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.server import (
    DEFAULT_COINS,
    EXCHANGE_NAME,
    _ensure_strategy,
    _load_funding_from_db,
    _resolve_exchange,
    build_app,
)
from frab.strategies.registry import get_strategy_spec


async def _factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    await init_db(engine)
    return engine, make_session_factory(engine)


async def test_ensure_strategy_creates_when_missing():
    engine, factory = await _factory()
    try:
        spec = get_strategy_spec("strategy_a")
        params = {"coins": list(DEFAULT_COINS), "concurrency_cap": 3}
        sid = await _ensure_strategy(factory, params, name=spec.name, version=spec.version)
        assert sid > 0

        async with session_scope(factory) as s:
            row = (await s.execute(
                select(Strategy).where(Strategy.id == sid)
            )).scalar_one()
            assert row.name == spec.name
            assert row.version == spec.version
            assert row.params_json == params
            assert row.status == "running"
            assert row.started_at is not None
    finally:
        await engine.dispose()


async def test_ensure_strategy_reuses_existing_row():
    engine, factory = await _factory()
    try:
        spec = get_strategy_spec("strategy_a")
        sid1 = await _ensure_strategy(factory, {"v": 1}, name=spec.name, version=spec.version)
        sid2 = await _ensure_strategy(factory, {"v": 2}, name=spec.name, version=spec.version)  # different params, same name/version
        assert sid1 == sid2

        async with session_scope(factory) as s:
            rows = (await s.execute(select(Strategy))).scalars().all()
            assert len(rows) == 1
            assert rows[0].status == "running"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_strategy_marks_status_running_and_cleans_up_leftovers():
    """_ensure_strategy must mark the active strategy as 'running' and sweep
    any previously-running strategies to 'stopped' (crash recovery)."""
    engine, factory = await _factory()
    try:
        # Seed a leftover 'running' strategy from a previous (crashed) process.
        spec = get_strategy_spec("strategy_a")
        async with session_scope(factory) as s:
            leftover = Strategy(
                name="other_strategy",
                version="v0",
                params_json={},
                status="running",
            )
            s.add(leftover)
            await s.flush()
            leftover_id = leftover.id

        sid = await _ensure_strategy(factory, {"v": 1}, name=spec.name, version=spec.version)

        async with session_scope(factory) as s:
            # The newly-ensured strategy must be running.
            active = (await s.get(Strategy, sid))
            assert active is not None
            assert active.status == "running"
            assert active.started_at is not None

            # The leftover must have been swept to stopped.
            old = (await s.get(Strategy, leftover_id))
            assert old is not None
            assert old.status == "stopped"
            assert old.stopped_at is not None
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


# ---------------------------------------------------------------------------
# _load_funding_from_db
# ---------------------------------------------------------------------------

async def _seed_funding(factory, exchange_id: int, coin: str, ticks: list[tuple[datetime, float]]) -> None:
    async with session_scope(factory) as s:
        market = (await s.execute(
            select(Market).where(Market.exchange_id == exchange_id, Market.coin == coin)
        )).scalar_one_or_none()
        if market is None:
            market = Market(exchange_id=exchange_id, coin=coin, min_size=0.01, tick_size=0.01)
            s.add(market)
            await s.flush()
        for ts, rate in ticks:
            s.add(FundingRate(
                market_id=market.id,
                ts=ts,
                rate=rate,
                premium=None,
                annualized_pct=rate * 8760 * 100,
            ))


@pytest.mark.asyncio
async def test_load_funding_from_db_primes_strategy_in_ascending_order(mocker):
    engine, factory = await _factory()
    try:
        async with session_scope(factory) as s:
            exc = Exchange(
                name=EXCHANGE_NAME, funding_interval_h=1,
                spot_taker_bps=7.0, perp_taker_bps=3.5,
            )
            s.add(exc)
            await s.flush()
            exchange_id = exc.id

        base = datetime(2026, 5, 15, 4, 0, tzinfo=UTC)
        await _seed_funding(factory, exchange_id, "BTC", [
            (base, 0.0001),
            (base + timedelta(hours=1), 0.0002),
            (base + timedelta(hours=2), 0.0003),
        ])
        await _seed_funding(factory, exchange_id, "ETH", [(base, 0.00005)])

        strategy = mocker.MagicMock()
        strategy.warmup_from_history = mocker.MagicMock(return_value=4)

        applied = await _load_funding_from_db(factory, strategy, ("BTC", "ETH"), window_hours=12)

        assert applied == 4
        passed = strategy.warmup_from_history.call_args.args[0]
        assert [t.ts for t in passed["BTC"]] == [
            base, base + timedelta(hours=1), base + timedelta(hours=2),
        ]
        # Re-attach UTC tzinfo even if DB stripped it
        assert all(t.ts.tzinfo == UTC for t in passed["BTC"])
        assert len(passed["ETH"]) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_funding_from_db_empty_db_returns_zero(mocker):
    engine, factory = await _factory()
    try:
        strategy = mocker.MagicMock()
        strategy.warmup_from_history = mocker.MagicMock(return_value=0)

        applied = await _load_funding_from_db(factory, strategy, ("BTC",), window_hours=12)

        assert applied == 0
        passed = strategy.warmup_from_history.call_args.args[0]
        assert passed == {"BTC": []}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_funding_from_db_respects_window_limit(mocker):
    engine, factory = await _factory()
    try:
        async with session_scope(factory) as s:
            exc = Exchange(
                name=EXCHANGE_NAME, funding_interval_h=1,
                spot_taker_bps=7.0, perp_taker_bps=3.5,
            )
            s.add(exc)
            await s.flush()
            exchange_id = exc.id

        base = datetime(2026, 5, 15, 4, 0, tzinfo=UTC)
        # Seed 5 rows but request window=3 — should return latest 3 only
        await _seed_funding(factory, exchange_id, "BTC", [
            (base + timedelta(hours=h), 0.0001 * h) for h in range(5)
        ])

        strategy = mocker.MagicMock()
        strategy.warmup_from_history = mocker.MagicMock(return_value=3)

        await _load_funding_from_db(factory, strategy, ("BTC",), window_hours=3)

        passed = strategy.warmup_from_history.call_args.args[0]
        assert len(passed["BTC"]) == 3
        # Latest 3 are hours 2,3,4 — ascending in output
        assert [t.ts for t in passed["BTC"]] == [
            base + timedelta(hours=2),
            base + timedelta(hours=3),
            base + timedelta(hours=4),
        ]
    finally:
        await engine.dispose()
