"""GET /api/b2/summary, /equity, /events over a book advanced by the paper engine."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from frab.api.app import create_app
from frab.db.models import Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.repo.b2_repo import B2Repo
from frab.strategy.b2.engine import HOUR_MS, B2PaperEngine
from frab.strategy.b2.params import B2Params
from frab.strategy.b2.tests.test_engine import T0, FakeClient


@pytest_asyncio.fixture
async def app_and_sid():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    sf = make_session_factory(eng)
    p = B2Params(coins=("BTC", "ETH"), capital_usd=2000.0, min_order_usd=0.0)
    async with session_scope(sf) as s:
        row = Strategy(name="b2", version="v2-paper", params_json=p.to_dict(), status="active")
        s.add(row)
        await s.flush()
        sid = row.id
    clock = {"now": T0 + 3000 * HOUR_MS + 90_000}
    engine = B2PaperEngine(session_factory=sf, client=FakeClient(), strategy_id=sid, clock=lambda: clock["now"])
    await engine.tick()
    clock["now"] += 5 * HOUR_MS
    await engine.tick()
    app = create_app(sf)
    app.state.b2_strategy_id = sid
    app.state.b2_engine = engine
    yield app, sid
    await eng.dispose()


@pytest.mark.asyncio
async def test_b2_summary_equity_events(app_and_sid):
    app, _ = app_and_sid
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        s = (await c.get("/api/b2/summary")).json()
        eq = (await c.get("/api/b2/equity")).json()
        ev = (await c.get("/api/b2/events")).json()
    assert s["mode"] == "paper" and s["capital"] == 2000.0
    assert all(coin["started"] for coin in s["coins"])
    assert abs(s["equity"] - sum(coin["equity"] for coin in s["coins"])) < 1e-9
    assert s["hours"] == len(eq) == 6
    assert any(e["kind"] == "init_spot_buy" for e in ev) and all(e["is_paper"] for e in ev)
    assert s["margin_enabled"] and s["apr_pct"] == pytest.approx(s["pnl_pct"] * 8760 / 6)
    btc = next(c for c in s["coins"] if c["coin"] == "BTC")
    assert btc["leverage"] == 3.0 and btc["spot_target"] + btc["hl_reserve"] == pytest.approx(1000.0)
    assert s["hl_account_value"] == pytest.approx(sum(c["hl_account_value"] for c in s["coins"]))


@pytest.mark.asyncio
async def test_b2_routes_503_when_engine_absent():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    app = create_app(make_session_factory(eng))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/b2/summary")).status_code == 503
    await eng.dispose()


@pytest.mark.asyncio
async def test_b2_routes_select_the_paper_test():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    sf = make_session_factory(eng)
    tests, engines = {}, {}
    clock = {"now": T0 + 3000 * HOUR_MS + 90_000}
    for name, p in (("b2", B2Params(coins=("BTC", "ETH"), capital_usd=2000.0, min_order_usd=0.0)),
                    ("b2_cold", B2Params(coins=("BTC", "ETH"), capital_usd=2000.0, min_order_usd=0.0,
                                         carry_enabled=False, hedge_margin_headroom=1.5))):
        async with session_scope(sf) as s:
            row = Strategy(name=name, version="v2-paper", params_json=p.to_dict(), status="active")
            s.add(row)
            await s.flush()
            sid = row.id
        engine = B2PaperEngine(session_factory=sf, client=FakeClient(), strategy_id=sid, clock=lambda: clock["now"])
        await engine.tick()
        tests[name], engines[sid] = sid, engine
    app = create_app(sf)
    app.state.b2_tests, app.state.b2_engines = tests, engines
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        main = (await c.get("/api/b2/summary")).json()
        cold = (await c.get("/api/b2/summary", params={"test": "b2_cold"})).json()
        cold_ev = (await c.get("/api/b2/events", params={"test": "b2_cold"})).json()
        missing = await c.get("/api/b2/summary", params={"test": "nope"})
    assert main["test"] == "b2" and main["params"]["carry_enabled"] is True
    assert cold["test"] == "b2_cold" and cold["params"]["carry_enabled"] is False
    assert cold["spot_value"] > main["spot_value"], "no carry -> more of the capital sits in spot"
    assert cold_ev and all(e["kind"] in ("init_spot_buy", "hedge_open") for e in cold_ev)
    assert missing.status_code == 404
    await eng.dispose()
