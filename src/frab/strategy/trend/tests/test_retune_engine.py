"""Re-tuned trend engine end to end on a fake Hyperliquid and a fake CoinGecko (no network)."""
from __future__ import annotations

import math

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Strategy, TrendEquity, TrendEvent
from frab.db.session import init_db, make_session_factory, session_scope
from frab.exchanges.hyperliquid.wire import HLCandle, HLFundingRecord
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.coin_selector import DAY_MS
from frab.strategy.trend.retune import RetuneState
from frab.strategy.trend.retune_book import RetuneBook
from frab.strategy.trend.retune_engine import HOUR_MS, TrendRetuneEngine
from frab.strategy.trend.retune_params import RetuneParams

T0 = 1_767_571_200_000                        # a Monday midnight
COINS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
PARAMS = RetuneParams.from_dict({
    "capital_usd": 10_000.0, "vol_window": 3, "min_history_days": 8, "risk_scale": 0.2,
    "lookback_sets": {"ens": [2, 4], "l2": [2], "l4": [4]}, "top_k_by_market_cap": 3,
    "top_k_by_volume": 3, "daily_fetch_pause_s": 0.0,
})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def boom(self, request, **kw):
        raise AssertionError(f"network access in a test: {request.url}")
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)


def price(coin: str, ms: float) -> float:
    i = COINS.index(coin)
    d = (ms - T0) / DAY_MS
    return 100.0 * (1 + (-1) ** i * 0.004 * (1 + i % 3)) ** d * (1 + 0.03 * math.sin(d * (0.7 + 0.31 * i) + i))


class FakeHL:
    def __init__(self, fail_daily_for: str | None = None):
        self.daily_calls: list[str] = []
        self.fail_daily_for = fail_daily_for

    async def info_request(self, body):
        assert body == {"type": "meta"}
        return {"universe": [{"name": c} for c in COINS] + [{"name": "DEAD", "isDelisted": True}]}

    async def candle_snapshot(self, coin, interval, start_ms, end_ms):
        step = HOUR_MS if interval == "1h" else DAY_MS
        if interval == "1d":
            self.daily_calls.append(coin)
            if coin == self.fail_daily_for:
                raise ConnectionError()
        i = COINS.index(coin)
        out = []
        for t in range(start_ms - start_ms % step, end_ms, step):
            if t < T0 - 60 * DAY_MS:
                continue
            # candles still open at end_ms are returned too, with a junk close, like the real API
            junk = t + step > end_ms
            out.append(HLCandle(coin=coin, open_ms=t, close_ms=t + step - 1,
                                close=price(coin, t + step) * (9.0 if junk else 1.0),
                                volume=1000.0 * (i + 1) * (1 + 0.2 * math.sin(t / DAY_MS + i))))
        return out

    async def funding_history(self, coin, since_ms):
        return [HLFundingRecord(coin=coin, ts_ms=t + 20, rate=0.00001, premium=0.0)
                for t in range(since_ms - since_ms % HOUR_MS, since_ms + 30 * DAY_MS, HOUR_MS)]


class FakeGecko:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def markets(self, *, per_page=250, category=None):
        if category:
            return [{"id": "tether"}]
        self.calls += 1
        if self.fail:
            raise RuntimeError("coingecko down")
        # market-cap order follows the coin list, a stablecoin on top
        rows = [{"id": "tether", "symbol": "usdt", "market_cap": 1e12, "market_cap_rank": 1}]
        rows += [{"id": c.lower(), "symbol": c.lower(), "market_cap": 1e11 / (i + 1), "market_cap_rank": i + 2}
                 for i, c in enumerate(COINS)]
        return rows

    async def aclose(self):
        pass


@pytest_asyncio.fixture
async def wired():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    sf = make_session_factory(engine)
    async with session_scope(sf) as s:
        row = Strategy(name="trend_retune", version="v1-paper", params_json=PARAMS.to_dict(), status="active")
        s.add(row)
        await s.flush()
        sid = row.id
    yield sf, sid
    await engine.dispose()


def make(sf, sid, now_ms, gecko=None, hl=None):
    return TrendRetuneEngine(session_factory=sf, client=hl or FakeHL(), strategy_id=sid,
                             repo=TrendRepo(sf, RetuneBook), clock=lambda: now_ms,
                             coingecko=gecko or FakeGecko())


async def run_days(sf, sid, last_day, gecko, step_days=3, start_day=70):
    """Tick every `step_days` days (each tick catches up the hours in between), up to `last_day`."""
    day = start_day
    while day <= last_day:
        await make(sf, sid, T0 + day * DAY_MS + 10 * HOUR_MS, gecko).tick()
        day += step_days


@pytest.mark.asyncio
async def test_first_tick_funds_the_book_and_waits_for_the_first_daily_bar(wired):
    sf, sid = wired
    out = await make(sf, sid, T0 + 70 * DAY_MS + 5 * HOUR_MS + 60_000).tick()
    assert out["hours"] == 1 and out["bars"] == 0
    book = await TrendRepo(sf, RetuneBook).get_book(sid)
    assert book.started and not book.units
    assert RetuneState.from_dict(book.retune, PARAMS).last_bar_ms is None      # nothing simulated before a rebalance bar


@pytest.mark.asyncio
async def test_a_daily_bar_simulates_all_candidates_and_persists_state_with_events(wired):
    sf, sid = wired
    gecko = FakeGecko()
    hl = FakeHL()
    await make(sf, sid, T0 + 70 * DAY_MS + 5 * HOUR_MS + 60_000, gecko, hl).tick()
    out = await make(sf, sid, T0 + 71 * DAY_MS + HOUR_MS + 200_000, gecko, hl).tick()
    assert out["bars"] == 1
    book = await TrendRepo(sf, RetuneBook).get_book(sid)
    st = RetuneState.from_dict(book.retune, PARAMS)
    assert st.last_bar_ms == T0 + 71 * DAY_MS and st.chosen is None and not book.units
    assert set(st.candidates) == {n for n, _, _ in PARAMS.candidates()}
    assert st.cap_snapshot["ranked"][0][0] == "AAA"                     # USDT excluded, market-cap order kept
    assert set(hl.daily_calls) == set(COINS)                            # every listed perp, never the delisted one
    async with sf() as s:
        ev = (await s.execute(TrendEvent.__table__.select().where(TrendEvent.kind == "retune_day"))).all()
    assert len(ev) == 1
    assert ev[0].details_json["universes"]["cap"] == ["AAA", "BBB", "CCC"]
    assert len(ev[0].details_json["universes"]["volume"]) == 3


@pytest.mark.asyncio
async def test_weeks_pass_retunes_are_recorded_and_the_book_trades_only_once_a_candidate_is_chosen(wired):
    sf, sid = wired
    gecko = FakeGecko()
    await make(sf, sid, T0 + 70 * DAY_MS + HOUR_MS + 60_000, gecko).tick()
    # advance day by day for five weeks, one tick a day
    for day in range(71, 71 + 36):
        await make(sf, sid, T0 + day * DAY_MS + HOUR_MS + 200_000, gecko).tick()
    repo = TrendRepo(sf, RetuneBook)
    book = await repo.get_book(sid)
    st = RetuneState.from_dict(book.retune, PARAMS)
    retunes = [e for e in await repo.events(sid, limit=1000, kinds=("retune",))][::-1]
    assert len(retunes) >= 4 and st.refits == len(retunes)
    d0, d1 = retunes[0].details_json, retunes[1].details_json
    assert d0["choice"] is None                                         # the first re-tune never chooses
    assert d1["choice"] is not None and set(d1["scores"]) == set(st.candidates)
    assert d1["score"] == d1["scores"][d1["choice"]]
    # before the second re-tune the book never held anything; afterwards it does
    first_trade_ms = min(e.ts_ms for e in await repo.events(sid, limit=5000) if e.kind in ("open", "increase"))
    assert first_trade_ms >= retunes[1].ts_ms - HOUR_MS
    assert book.units and set(book.units) <= set(COINS)
    # market caps were fetched once per ISO week, not every day
    assert 4 <= gecko.calls <= 7
    eq = await repo.equity_series(sid)
    assert len({t for t, _ in eq}) == len(eq) and all(e > 0 for _, e in eq)


@pytest.mark.asyncio
async def test_a_restart_resumes_from_the_stored_state_and_matches_an_uninterrupted_run(wired):
    sf, sid = wired
    gecko = FakeGecko()
    for day in range(70, 100):
        await make(sf, sid, T0 + day * DAY_MS + HOUR_MS + 200_000, gecko).tick()      # a "new process" every tick
    book = await TrendRepo(sf, RetuneBook).get_book(sid)

    engine2 = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine2)
    sf2 = make_session_factory(engine2)
    async with session_scope(sf2) as s:
        s.add(Strategy(name="trend_retune", version="v1-paper", params_json=PARAMS.to_dict(), status="active"))
    one_engine = make(sf2, 1, 0, FakeGecko())
    for day in range(70, 100):
        one_engine._clock = (lambda d=day: T0 + d * DAY_MS + HOUR_MS + 200_000)
        await one_engine.tick()
    book2 = await TrendRepo(sf2, RetuneBook).get_book(1)
    assert book.retune == book2.retune and book.units == book2.units
    await engine2.dispose()


@pytest.mark.asyncio
async def test_catching_up_after_downtime_replays_each_missed_bar_without_a_fresh_market_cap_for_old_ones(wired):
    sf, sid = wired
    gecko = FakeGecko()
    await make(sf, sid, T0 + 70 * DAY_MS + HOUR_MS + 60_000, gecko).tick()
    await make(sf, sid, T0 + 71 * DAY_MS + HOUR_MS + 60_000, gecko).tick()
    calls = gecko.calls
    out = await make(sf, sid, T0 + 76 * DAY_MS + HOUR_MS + 60_000, gecko).tick()     # five days offline
    assert out["bars"] == 5
    book = await TrendRepo(sf, RetuneBook).get_book(sid)
    st = RetuneState.from_dict(book.retune, PARAMS)
    assert st.last_bar_ms == T0 + 76 * DAY_MS
    # the stale bars must not have taken today's ranking: at most the one fresh bar refreshes it
    assert gecko.calls - calls <= 1


@pytest.mark.asyncio
async def test_failed_market_cap_source_defers_the_first_tick_but_a_known_ranking_is_kept(wired):
    sf, sid = wired
    await make(sf, sid, T0 + 70 * DAY_MS + 5 * HOUR_MS + 60_000).tick()
    with pytest.raises(RuntimeError, match="market caps unavailable"):
        await make(sf, sid, T0 + 71 * DAY_MS + HOUR_MS + 60_000, FakeGecko(fail=True)).tick()
    nothing = RetuneState.from_dict((await TrendRepo(sf, RetuneBook).get_book(sid)).retune, PARAMS)
    assert nothing.last_bar_ms is None and nothing.cap_snapshot is None               # nothing advanced

    await make(sf, sid, T0 + 71 * DAY_MS + HOUR_MS + 60_000, FakeGecko()).tick()      # source back
    # a later week with the source down: keep last week's ranking, keep going
    out = await make(sf, sid, T0 + 80 * DAY_MS + HOUR_MS + 60_000, FakeGecko(fail=True)).tick()
    assert out["bars"] >= 1


@pytest.mark.asyncio
async def test_a_failed_daily_fetch_defers_the_tick_and_the_next_replays_it(wired):
    sf, sid = wired
    await make(sf, sid, T0 + 70 * DAY_MS + 5 * HOUR_MS + 60_000).tick()
    with pytest.raises(RuntimeError, match="CCC"):
        await make(sf, sid, T0 + 71 * DAY_MS + HOUR_MS + 60_000, hl=FakeHL(fail_daily_for="CCC")).tick()
    book = await TrendRepo(sf, RetuneBook).get_book(sid)
    assert book.last_bar_ms == T0 + 70 * DAY_MS + 4 * HOUR_MS                       # the hours were not consumed
    assert RetuneState.from_dict(book.retune, PARAMS).last_bar_ms is None
    out = await make(sf, sid, T0 + 71 * DAY_MS + HOUR_MS + 60_000).tick()
    assert out["bars"] == 1


@pytest.mark.asyncio
async def test_a_paused_strategy_does_not_advance(wired):
    sf, sid = wired
    async with session_scope(sf) as s:
        (await s.get(Strategy, sid)).status = "paused"
    assert (await make(sf, sid, T0 + 71 * DAY_MS).tick())["hours"] == 0
    assert await TrendRepo(sf, RetuneBook).get_book(sid) is None


@pytest.mark.asyncio
async def test_the_plain_trend_repo_still_reads_a_book_written_without_selector_state(wired):
    sf, sid = wired
    await make(sf, sid, T0 + 70 * DAY_MS + 5 * HOUR_MS + 60_000).tick()
    plain = await TrendRepo(sf).get_book(sid)                    # the default repo reads the same row as a TrendBook
    assert type(plain).__name__ == "TrendBook" and plain.started
