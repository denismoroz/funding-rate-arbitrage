"""B2 paper engine: catch-up, idempotency, gaps, pause, and equality with direct stepping."""
from __future__ import annotations

import math

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import B2Equity, B2Event, Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.exchanges.hyperliquid.wire import HLCandle, HLFundingRecord
from frab.repo.b2_repo import B2Repo
from frab.strategy.b2.book import CoinBook, advance_signals, start_book, step
from frab.strategy.b2.engine import HISTORY_BARS, HOUR_MS, WARMUP_BARS, B2PaperEngine
from frab.strategy.b2.params import B2Params

T0 = 1_780_000_000_000 - 1_780_000_000_000 % HOUR_MS


def price(h: int, coin: str) -> float:
    base = {"BTC": 60000.0, "ETH": 3000.0}[coin]
    # a fall, then a recovery: forces hedge open, sticky hold, exit, refill
    return base * (1 + 0.25 * math.sin(h / 400.0) + 0.01 * math.sin(h / 7.0))


class FakeClient:
    def __init__(self, missing: set[int] | None = None):
        self.missing = missing or set()
        self.calls = 0

    async def candle_snapshot(self, coin, interval, start_ms, end_ms):
        self.calls += 1
        out = []
        for t in range(start_ms, end_ms, HOUR_MS):
            if t in self.missing or t + HOUR_MS > end_ms:
                continue
            h = (t - T0) // HOUR_MS
            out.append(HLCandle(coin=coin, open_ms=t, close_ms=t + HOUR_MS, close=price(h, coin)))
        return out

    async def funding_history(self, coin, since_ms):
        return [HLFundingRecord(coin=coin, ts_ms=t + 7, rate=0.00002 + 0.00003 * math.sin((t - T0) / HOUR_MS / 50),
                                premium=0.0) for t in range(since_ms, T0 + 5000 * HOUR_MS, HOUR_MS)]


@pytest_asyncio.fixture
async def sf():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    f = make_session_factory(eng)
    yield f
    await eng.dispose()


async def _strategy(sf, status="active", **params):
    p = B2Params(coins=("BTC", "ETH"), capital_usd=2000.0, min_order_usd=0.0, **params)
    async with session_scope(sf) as s:
        row = Strategy(name="b2", version="v2", params_json=p.to_dict(), status=status)
        s.add(row)
        await s.flush()
        return row.id, p


def _engine(sf, sid, client, now):
    clock = {"now": now}
    eng = B2PaperEngine(session_factory=sf, client=client, strategy_id=sid, clock=lambda: clock["now"])
    return eng, clock


@pytest.mark.asyncio
async def test_first_tick_starts_then_catches_up_without_double_counting(sf):
    sid, _ = await _strategy(sf)
    start = T0 + 3000 * HOUR_MS
    eng, clock = _engine(sf, sid, FakeClient(), start + 90_000)
    r1 = await eng.tick()
    assert r1["processed"] == {"BTC": 1, "ETH": 1}
    # tick 1 took the newest closed bar (open = start-1h); now bars start..start+5h have closed
    clock["now"] = start + 6 * HOUR_MS + 90_000
    r2 = await eng.tick()
    assert r2["processed"] == {"BTC": 6, "ETH": 6}
    r3 = await eng.tick()                                   # same hour again: nothing new
    assert r3["processed"] == {"BTC": 0, "ETH": 0}
    async with sf() as s:
        n = (await s.execute(B2Equity.__table__.select().where(B2Equity.strategy_id == sid))).all()
    assert len(n) == 14, "one snapshot per coin per closed bar (1 + 6), no duplicates"


@pytest.mark.asyncio
async def test_engine_equals_direct_stepping(sf):
    """Hourly ticks through the engine must equal one continuous run of the book."""
    sid, p = await _strategy(sf)
    start = T0 + 3000 * HOUR_MS
    client = FakeClient()
    eng, clock = _engine(sf, sid, client, start + 90_000)
    for k in range(0, 400, 7):                              # irregular tick spacing
        clock["now"] = start + (k + 1) * HOUR_MS + 90_000
        await eng.tick()
    book = await B2Repo(sf).get_book(sid, "BTC")

    hist = start - (HISTORY_BARS + WARMUP_BARS) * HOUR_MS
    end = start + 399 * HOUR_MS              # last tick: now = start+400h -> last closed bar = start+399h
    hours = list(range(hist, end + HOUR_MS, HOUR_MS))
    px = [price((h - T0) // HOUR_MS, "BTC") for h in hours]
    fr = [0.00002 + 0.00003 * math.sin((h - T0) / HOUR_MS / 50) for h in hours]
    ref = CoinBook.new("BTC", p)
    i0 = hours.index(start)
    for i in range(i0 - WARMUP_BARS, i0):
        advance_signals(ref, px[max(0, i - HISTORY_BARS + 9):i + 1], fr[max(0, i - 8):i + 1], p)
    start_book(ref, bar_ms=start, price=px[i0], params=p)
    kinds = set()
    for i in range(i0, len(hours)):
        kinds |= {e["kind"] for e in step(ref, bar_ms=hours[i], price=px[i], funding_rate=fr[i],
                                          closes=px[max(0, i - HISTORY_BARS + 9):i + 1],
                                          funding_hist=fr[max(0, i - 8):i + 1], params=p)}
    assert book.last_bar_ms == ref.last_bar_ms
    assert abs(book.equity(ref.last_price) - ref.equity(ref.last_price)) < 1e-9
    assert {"hedge_open"} <= kinds, "synthetic path should exercise the hedge"


@pytest.mark.asyncio
async def test_missing_candle_waits_instead_of_fabricating(sf):
    sid, _ = await _strategy(sf)
    start = T0 + 3000 * HOUR_MS
    eng, clock = _engine(sf, sid, FakeClient(), start + 90_000)
    await eng.tick()
    gap = start + 2 * HOUR_MS
    eng._client = FakeClient(missing={gap})
    clock["now"] = start + 4 * HOUR_MS + 90_000
    r = await eng.tick()
    assert r["processed"]["BTC"] == 0, "a gap in traded bars must stall the book, not skip the hour"


@pytest.mark.asyncio
async def test_paused_strategy_does_nothing(sf):
    sid, _ = await _strategy(sf, status="paused")
    client = FakeClient()
    eng, _ = _engine(sf, sid, client, T0 + 3000 * HOUR_MS + 90_000)
    r = await eng.tick()
    assert r["processed"] == {} and client.calls == 0
