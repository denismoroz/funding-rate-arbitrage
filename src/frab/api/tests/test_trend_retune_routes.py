"""GET /api/trend/*?variant=trend_retune over a book advanced by the re-tuned paper engine."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from frab.api.app import create_app
from frab.db.models import Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.coin_selector import DAY_MS
from frab.strategy.trend.retune_book import RetuneBook
from frab.strategy.trend.retune_engine import HOUR_MS, TrendRetuneEngine
from frab.strategy.trend.tests.test_retune_engine import PARAMS, T0, FakeGecko, FakeHL


@pytest_asyncio.fixture
async def app_and_sid():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    sf = make_session_factory(eng)
    async with session_scope(sf) as s:
        row = Strategy(name="trend_retune", version="v1-paper", params_json=PARAMS.to_dict(), status="active")
        s.add(row)
        await s.flush()
        sid = row.id
    clock = {"now": T0 + 70 * DAY_MS + HOUR_MS + 90_000}
    engine = TrendRetuneEngine(session_factory=sf, client=FakeHL(), strategy_id=sid,
                               repo=TrendRepo(sf, RetuneBook), clock=lambda: clock["now"], coingecko=FakeGecko())
    for _ in range(30):
        await engine.tick()
        clock["now"] += DAY_MS
    app = create_app(sf)
    app.state.trend_retune_strategy_id = sid
    app.state.trend_retune_engine = engine
    yield app
    await eng.dispose()


@pytest.mark.asyncio
async def test_retune_variant_summary_retunes_and_default_variant_unaffected(app_and_sid):
    app = app_and_sid
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        s = (await c.get("/api/trend/summary", params={"variant": "trend_retune"})).json()
        rt = (await c.get("/api/trend/retunes")).json()          # always reads the retune variant
        eq = (await c.get("/api/trend/equity", params={"variant": "trend_retune"})).json()
        ev = (await c.get("/api/trend/events", params={"variant": "trend_retune"})).json()
        unknown = (await c.get("/api/trend/summary", params={"variant": "nope"})).status_code
        plain = (await c.get("/api/trend/summary")).status_code

    assert s["status"] == "active" and s["started"] and s["capital"] == PARAMS.trend.capital_usd
    assert s["coins"] == [] and s["retune"]["refits"] >= 3 and s["retune"]["chosen"] is not None
    assert {c["name"] for c in s["retune"]["candidates"]} == {n for n, _, _ in PARAMS.candidates()}
    assert s["retune"]["cap_snapshot_week"] and s["universe"]
    assert abs(s["equity"] - (s["cash"] + s["unrealized"])) < 1e-9
    assert eq[-1]["equity"] == s["equity"]
    assert {e["kind"] for e in ev} >= {"retune_day", "retune", "fund"}
    assert len(rt) == s["retune"]["refits"] and all(set(r["details"]["scores"]) for r in rt)
    assert unknown == 404 and plain == 503
