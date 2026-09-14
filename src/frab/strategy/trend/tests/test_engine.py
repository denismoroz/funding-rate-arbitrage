"""Trend paper engine: catch-up, one rebalance a day, pause, and the history gate."""
from __future__ import annotations

import math

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Strategy, TrendEquity, TrendEvent
from frab.db.session import init_db, make_session_factory, session_scope
from frab.exchanges.hyperliquid.wire import HLCandle, HLFundingRecord
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.engine import DAY_MS, HOUR_MS, TrendPaperEngine
from frab.strategy.trend.params import TrendParams

T0 = 1_780_000_000_000 - 1_780_000_000_000 % DAY_MS           # a midnight
PARAMS = TrendParams(coins=("BTC", "ETH", "NEW"), capital_usd=1000.0, lookbacks=(2, 4), vol_window=3,
                     min_history_days=6, risk_scale=0.2)


def price(coin: str, ms: int) -> float:
    day = (ms - T0) / DAY_MS
    base = {"BTC": 50_000.0, "ETH": 2_000.0, "NEW": 10.0}[coin]
    return base * (1 + 0.02 * day + 0.01 * math.sin(day * 3))      # a clean uptrend plus wiggle


class FakeClient:
    """BTC and ETH have years of daily history; NEW listed three days ago."""

    def __init__(self) -> None:
        self.calls = 0

    async def candle_snapshot(self, coin, interval, start_ms, end_ms):
        self.calls += 1
        step = HOUR_MS if interval == "1h" else DAY_MS
        first_listed = T0 - 3 * DAY_MS if coin == "NEW" else T0 - 400 * DAY_MS
        out = []
        for t in range(start_ms - start_ms % step, end_ms, step):
            if t < first_listed or t + step > end_ms:
                continue
            out.append(HLCandle(coin=coin, open_ms=t, close_ms=t + step, close=price(coin, t + step)))
        return out

    async def funding_history(self, coin, since_ms):
        return [HLFundingRecord(coin=coin, ts_ms=t + 11, rate=0.00001, premium=0.0)
                for t in range(since_ms - since_ms % HOUR_MS, since_ms + 400 * HOUR_MS, HOUR_MS)]


@pytest_asyncio.fixture
async def wired():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    sf = make_session_factory(engine)
    async with session_scope(sf) as s:
        row = Strategy(name="trend", version="v1-paper", params_json=PARAMS.to_dict(), status="active")
        s.add(row)
        await s.flush()
        sid = row.id
    yield sf, sid
    await engine.dispose()


def make(sf, sid, now_ms):
    return TrendPaperEngine(session_factory=sf, client=FakeClient(), strategy_id=sid,
                            repo=TrendRepo(sf), clock=lambda: now_ms)


@pytest.mark.asyncio
async def test_first_tick_starts_the_book_and_only_trades_at_the_rebalance_hour(wired):
    sf, sid = wired
    eng = make(sf, sid, T0 + 5 * HOUR_MS + 60_000)              # 05:xx UTC
    out = await eng.tick()
    assert out["hours"] == 1
    book = await TrendRepo(sf).get_book(sid)
    assert book.started and book.rebalances == 1                # a fresh book sizes itself at once

    eng2 = make(sf, sid, T0 + DAY_MS + HOUR_MS + 60_000)        # next day 01:xx -> 00:00 was a rebalance
    out2 = await eng2.tick()
    assert out2["hours"] == 20
    book = await TrendRepo(sf).get_book(sid)
    assert book.rebalances == 2                                 # plus the 00:00 UTC one
    assert set(book.units) == {"BTC", "ETH"}                    # NEW lacks history -> no position
    assert all(u > 0 for u in book.units.values())              # both in an uptrend -> long
    assert book.last_bar_ms == T0 + DAY_MS + 0 * HOUR_MS


@pytest.mark.asyncio
async def test_gross_matches_the_capped_book_and_funding_is_charged(wired):
    sf, sid = wired
    eng = make(sf, sid, T0 + DAY_MS + 2 * HOUR_MS + 60_000)
    await eng.tick()                                            # funds and sizes the book
    eng = make(sf, sid, T0 + DAY_MS + 4 * HOUR_MS + 60_000)
    await eng.tick()                                            # holds it for two more hours
    book = await TrendRepo(sf).get_book(sid)
    cap = PARAMS.leverage_cap * PARAMS.risk_scale
    prior_scale = PARAMS.book_vol_target_ann / PARAMS.book_vol_prior_ann   # no own history yet
    assert book.size_scale == prior_scale
    assert sum(abs(w) for w in book.weights.values()) <= cap * prior_scale + 1e-9
    gross = book.gross_notional(book.prices) / book.equity(book.prices)
    assert gross <= cap * prior_scale * 1.05                               # drifts with prices between rebalances
    assert book.funding_total < 0                               # long book pays a positive rate
    assert book.fees > 0
    assert eng.universe == ["BTC", "ETH"]


@pytest.mark.asyncio
async def test_catch_up_is_idempotent_and_writes_one_equity_row_per_hour(wired):
    sf, sid = wired
    now = T0 + 2 * DAY_MS + 3 * HOUR_MS + 60_000
    await make(sf, sid, now).tick()
    again = await make(sf, sid, now).tick()
    assert again["hours"] == 0
    async with sf() as s:
        rows = (await s.execute(TrendEquity.__table__.select())).all()
        fills = (await s.execute(TrendEvent.__table__.select())).all()
    ts = sorted(r.ts_ms for r in rows)
    assert len(ts) == len(set(ts))
    assert ts[-1] - ts[0] == (len(ts) - 1) * HOUR_MS
    assert {f.kind for f in fills} >= {"fund", "open"}   # the fresh book funds and sizes itself
    assert all(f.is_paper for f in fills)


@pytest.mark.asyncio
async def test_a_paused_strategy_does_not_advance(wired):
    sf, sid = wired
    async with session_scope(sf) as s:
        (await s.get(Strategy, sid)).status = "paused"
    out = await make(sf, sid, T0 + DAY_MS + 60_000).tick()
    assert out["hours"] == 0
    assert await TrendRepo(sf).get_book(sid) is None
