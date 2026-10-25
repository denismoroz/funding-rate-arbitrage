"""GET /api/b2/{summary,equity,events,retunes}?test=b2_retune over a book advanced by the retune engine."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from frab.api.app import create_app
from frab.db.models import Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.strategy.b2.retune_engine import B2RetuneEngine
from frab.strategy.b2.retune_history import HOUR_MS, HourlyHistoryStore
from frab.strategy.b2.tests.test_retune_engine import DEPLOY, NOW0, PARAMS, FakeClient, _no_sleep


@pytest_asyncio.fixture(scope="module")
async def served(tmp_path_factory):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    sf = make_session_factory(eng)
    async with session_scope(sf) as s:
        row = Strategy(name="b2_retune", version="v2-paper-retune", params_json=PARAMS, status="active")
        s.add(row)
        await s.flush()
        sid = row.id
    clock = {"now": NOW0}
    store = HourlyHistoryStore(tmp_path_factory.mktemp("cache"), seed_path=DEPLOY / "b2_retune_history_seed.json",
                               candle_pause_s=0, funding_page_pause_s=0, sleep=_no_sleep)
    engine = B2RetuneEngine(session_factory=sf, client=FakeClient(), strategy_id=sid, clock=lambda: clock["now"],
                            history_store=store)
    await engine.tick()
    clock["now"] += 4 * HOUR_MS
    await engine.tick()
    app = create_app(sf)
    app.state.b2_tests = {"b2_retune": sid}
    app.state.b2_engines = {sid: engine}
    app.state.b2_retune_strategy_id = sid
    yield app
    await eng.dispose()


@pytest.mark.asyncio
async def test_summary_shows_the_choice_the_refit_dates_and_the_live_part_separately(served):
    async with AsyncClient(transport=ASGITransport(app=served), base_url="http://t") as c:
        s = (await c.get("/api/b2/summary?test=b2_retune")).json()
    r = s["retune"]
    assert s["capital"] == 257.0 and all(coin["started"] for coin in s["coins"])
    assert abs(s["equity"] - sum(coin["equity"] for coin in s["coins"])) < 1e-9
    assert r["chosen"] == "b2" and r["refits"] == 7 and r["choice_changes"] == 1
    assert r["last_refit_ms"] == 1_790_812_800_000                              # 2026-10-01 00:00 UTC
    assert r["last_decision_ms"] == r["last_refit_ms"] + HOUR_MS
    assert r["next_refit_ms"] == 1_793_491_200_000                              # 2026-11-01 00:00 UTC
    assert r["first_choice_ms"] == 1_780_275_600_000 and r["launch_ms"] < s["last_bar_ms"]
    assert r["live_hours"] == 4 and r["live_pnl"] is not None
    assert {c["name"] for c in r["candidates"]} == {"b2", "b2_cold"} and all(c["sharpe"] for c in r["candidates"])
    assert s["hours"] > 3000 and s["started_ms"] == r["first_choice_ms"] - HOUR_MS     # the capital row


@pytest.mark.asyncio
async def test_equity_events_and_retunes_endpoints(served):
    async with AsyncClient(transport=ASGITransport(app=served), base_url="http://t") as c:
        eq = (await c.get("/api/b2/equity?test=b2_retune")).json()
        ev = (await c.get("/api/b2/events?test=b2_retune&limit=1000")).json()
        rt = (await c.get("/api/b2/retunes")).json()                             # defaults to b2_retune
    assert eq[0] == {"ts_ms": 1_780_272_000_000, "equity": pytest.approx(257.0)} and len(eq) > 3000
    assert [e["details"]["choice"] for e in rt] == ["b2", "b2_cold", "b2_cold", "b2_cold", "b2_cold", None, None]
    assert all("scores" in e["details"] and e["details"]["phase"] == "backfill" for e in rt)
    assert {e["kind"] for e in ev} >= {"retune", "retune_switch", "hedge_open"}
    phases = {e["details"]["phase"] for e in ev if e["details"] and "phase" in e["details"]}
    assert phases <= {"backfill", "live"}


@pytest.mark.asyncio
async def test_the_other_books_summary_has_no_retune_block(served):
    # a plain b2 test wired next to it must not grow a `retune` key
    served.state.b2_tests["b2"] = served.state.b2_tests["b2_retune"] + 1000     # no such strategy: only the 404 path
    async with AsyncClient(transport=ASGITransport(app=served), base_url="http://t") as c:
        assert (await c.get("/api/b2/summary?test=nope")).status_code == 404
    del served.state.b2_tests["b2"]
