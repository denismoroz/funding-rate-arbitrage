"""?variant=<name> for several re-tuned books running side by side (T52b)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from frab.api.app import create_app
from frab.db.session import init_db, make_session_factory
from frab.strategy.trend.tests.test_retune_backfill_engine import (
    FIRST_HOUR, HOUR_MS, LAUNCH, add_row, make)


@pytest_asyncio.fixture
async def app_with_two_books(tmp_path):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    sf = make_session_factory(eng)
    small = await add_row(sf, "trend_retune_3k", capital_usd=3_000.0)
    big = await add_row(sf, "trend_retune_10k", capital_usd=10_000.0)
    engines = {}
    for name, sid in (("trend_retune_3k", small), ("trend_retune_10k", big)):
        engines[name] = make(sf, sid, LAUNCH, tmp_path)
        await engines[name].tick()
    app = create_app(sf)
    app.state.trend_retune_variants = {n: {"strategy_id": sid, "engine": engines[n]}
                                       for n, sid in (("trend_retune_3k", small), ("trend_retune_10k", big))}
    yield app
    await eng.dispose()


@pytest.mark.asyncio
async def test_each_variant_has_its_own_summary_capital_state_and_first_choice(app_with_two_books):
    async with AsyncClient(transport=ASGITransport(app=app_with_two_books), base_url="http://t") as c:
        small = (await c.get("/api/trend/summary", params={"variant": "trend_retune_3k"})).json()
        big = (await c.get("/api/trend/summary", params={"variant": "trend_retune_10k"})).json()
        missing = await c.get("/api/trend/summary", params={"variant": "trend_retune_1m"})
        plain = await c.get("/api/trend/summary")
    assert small["capital"] == 3_000.0 and big["capital"] == 10_000.0
    assert small["variant"] == "trend_retune_3k" and big["variant"] == "trend_retune_10k"
    assert small["first_choice_at"] == big["first_choice_at"] == FIRST_HOUR + HOUR_MS
    assert small["retune"]["first_choice_at"] == FIRST_HOUR + HOUR_MS
    assert small["retune"]["days_since_first_choice"] is not None
    assert small["retune"]["chosen"] == big["retune"]["chosen"] is not None
    assert small["retune"]["backfill"]["bars"] == 120 and small["retune"]["cap_sources"]
    assert set(small["retune"]["cap_sources"].values()) == {"cmc_history"}
    assert small["equity"] != big["equity"] and small["positions"] and big["positions"]
    assert big["gross_notional"] > 2.5 * small["gross_notional"]
    assert missing.status_code == 503 and plain.status_code == 503      # no plain trend engine in this app


@pytest.mark.asyncio
async def test_equity_events_and_retunes_are_read_per_variant(app_with_two_books):
    async with AsyncClient(transport=ASGITransport(app=app_with_two_books), base_url="http://t") as c:
        eq_s = (await c.get("/api/trend/equity", params={"variant": "trend_retune_3k"})).json()
        eq_b = (await c.get("/api/trend/equity", params={"variant": "trend_retune_10k"})).json()
        ev_b = (await c.get("/api/trend/events", params={"variant": "trend_retune_10k", "limit": 1000})).json()
        rt_b = (await c.get("/api/trend/retunes", params={"variant": "trend_retune_10k"})).json()
        rt_s = (await c.get("/api/trend/retunes", params={"variant": "trend_retune_3k"})).json()
        ambiguous = await c.get("/api/trend/retunes")                   # two variants, none named plain trend_retune
    assert len(eq_s) == len(eq_b) == 1 and eq_s[0]["equity"] < 3_100.0 < 9_000.0 < eq_b[0]["equity"]
    assert any(e["kind"] == "backfill" for e in ev_b) and any(e["kind"] == "open" for e in ev_b)
    assert [r["details"]["phase"] for r in rt_b].count("launch") == 1
    assert [r["details"]["week"] for r in rt_b] == [r["details"]["week"] for r in rt_s]
    assert ambiguous.status_code == 503


@pytest.mark.asyncio
async def test_without_a_variant_the_only_running_one_answers_the_retunes_route(tmp_path):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    sf = make_session_factory(eng)
    sid = await add_row(sf, "trend_retune_10k")
    engine = make(sf, sid, LAUNCH, tmp_path)
    await engine.tick()
    app = create_app(sf)
    app.state.trend_retune_variants = {"trend_retune_10k": {"strategy_id": sid, "engine": engine}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        rt = (await c.get("/api/trend/retunes")).json()
    assert rt and rt[0]["details"]["phase"] in ("launch", "history")
    await eng.dispose()
