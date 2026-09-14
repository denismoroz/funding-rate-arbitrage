"""GET /api/trend/summary, /equity, /events over a book advanced by the paper engine."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from frab.api.app import create_app
from frab.db.models import Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.strategy.trend.engine import DAY_MS, HOUR_MS, TrendPaperEngine
from frab.strategy.trend.tests.test_engine import PARAMS, T0, FakeClient


@pytest_asyncio.fixture
async def app_and_sid():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    sf = make_session_factory(eng)
    async with session_scope(sf) as s:
        row = Strategy(name="trend", version="v1-paper", params_json=PARAMS.to_dict(), status="active")
        s.add(row)
        await s.flush()
        sid = row.id
    clock = {"now": T0 + DAY_MS + HOUR_MS + 90_000}
    engine = TrendPaperEngine(session_factory=sf, client=FakeClient(), strategy_id=sid,
                              clock=lambda: clock["now"])
    await engine.tick()
    clock["now"] += 5 * HOUR_MS
    await engine.tick()
    app = create_app(sf)
    app.state.trend_strategy_id = sid
    app.state.trend_engine = engine
    yield app, sid
    await eng.dispose()


@pytest.mark.asyncio
async def test_summary_equity_events(app_and_sid):
    app, _ = app_and_sid
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        s = (await c.get("/api/trend/summary")).json()
        eq = (await c.get("/api/trend/equity")).json()
        ev = (await c.get("/api/trend/events")).json()

    assert s["status"] == "active" and s["started"] and s["capital"] == PARAMS.capital_usd
    assert s["universe"] == ["BTC", "ETH"]                       # NEW has too little history
    assert s["legs"] == len([p for p in s["positions"] if p["units"]])
    assert abs(s["equity"] - (s["cash"] + s["unrealized"])) < 1e-9
    assert s["gross_leverage"] <= PARAMS.leverage_cap * PARAMS.risk_scale * 1.05
    for p in s["positions"]:
        assert p["side"] == ("long" if p["units"] > 0 else "short" if p["units"] < 0 else "flat")
        assert p["signal"] is None or -1.0 <= p["signal"] <= 1.0

    assert len(eq) == len(s and eq) and eq == sorted(eq, key=lambda r: r["ts_ms"])
    assert eq[-1]["equity"] == s["equity"]
    assert {e["kind"] for e in ev} >= {"fund", "open"}
    assert all(e["notional"] >= 0 for e in ev)


@pytest.mark.asyncio
async def test_summary_503_without_an_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    app = create_app(make_session_factory(eng))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/trend/summary")).status_code == 503
    await eng.dispose()
