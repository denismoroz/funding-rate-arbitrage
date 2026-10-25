"""The b2_retune engine: first-tick replay (backfill), live ticks, restart, incremental history, opt-in.

The integration tests run on the real seed (`deploy/b2_retune_history_seed.json`, the q-lab stand's hours from
2026-03-08 to 2026-10-24 00:00); hours after it come from a deterministic fake client.
"""
from __future__ import annotations

import json
import math
import os

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import B2Book, B2Equity, B2Event, Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.exchanges.hyperliquid.wire import HLCandle, HLFundingRecord
from frab.repo.b2_repo import RETUNE_STATE_COIN, B2Repo
from frab.repo.b2_retune_repo import B2RetuneRepo
from frab.strategy.b2.retune import RetuneState
from frab.strategy.b2.retune_engine import REPO_ROOT, B2RetuneEngine
from frab.strategy.b2.retune_history import HOUR_MS, HourlyHistoryStore
from frab.strategy.b2.retune_params import B2RetuneParams

DEPLOY = REPO_ROOT / "deploy"
SEED = json.loads((DEPLOY / "b2_retune_history_seed.json").read_text())
SEED_END = SEED["last_open_ms"] + HOUR_MS                  # first hour after the seed (2026-10-24 01:00)
PARAMS = json.loads((DEPLOY / "b2_retune_params.json").read_text())


def _px(coin: str, ms: int) -> float:
    """Hours after the seed: a quiet walk from the seed's last close."""
    base = SEED["coins"][coin]["close"][-1]
    h = (ms - SEED["last_open_ms"]) // HOUR_MS
    return base * (1 + 0.004 * math.sin(h / 9.0))


class FakeClient:
    def __init__(self) -> None:
        self.candle_calls: list[tuple[str, int, int]] = []
        self.funding_calls: list[tuple[str, int]] = []

    async def candle_snapshot(self, coin, interval, start_ms, end_ms):
        assert interval == "1h"
        self.candle_calls.append((coin, start_ms, end_ms))
        out = []
        for t in range(start_ms - start_ms % HOUR_MS, end_ms + 1, HOUR_MS):
            if t < SEED["origin_ms"]:
                continue
            i = (t - SEED["origin_ms"]) // HOUR_MS
            if t <= SEED["last_open_ms"]:
                close, high = SEED["coins"][coin]["close"][i], SEED["coins"][coin]["high"][i]
            else:
                close = _px(coin, t)
                high = close * 1.001
            out.append(HLCandle(coin=coin, open_ms=t, close_ms=t + HOUR_MS, close=close, high=high))
        return out

    async def funding_history(self, coin, since_ms, *, end_ms=None, page_pause_s=0.0):
        self.funding_calls.append((coin, since_ms))
        out = []
        for t in range(since_ms - since_ms % HOUR_MS, (end_ms or since_ms) + 1, HOUR_MS):
            if t < SEED["origin_ms"]:
                continue
            i = (t - SEED["origin_ms"]) // HOUR_MS
            rate = SEED["coins"][coin]["funding"][i] if t <= SEED["last_open_ms"] else 0.00001
            out.append(HLFundingRecord(coin=coin, ts_ms=t + 7, rate=rate, premium=0.0))
        return out


async def _no_sleep(_s: float) -> None:
    return None


@pytest_asyncio.fixture
async def sf():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    f = make_session_factory(eng)
    yield f
    await eng.dispose()


async def _add_strategy(sf, params: dict | None = None, status: str = "active", name: str = "b2_retune") -> int:
    async with session_scope(sf) as s:
        row = Strategy(name=name, version="v2-paper-retune", params_json=params or PARAMS, status=status)
        s.add(row)
        await s.flush()
        return row.id


def _engine(sf, sid, client, now, tmp_path, *, seed=True) -> tuple[B2RetuneEngine, dict]:
    clock = {"now": now}
    p = B2RetuneParams.from_dict(PARAMS)
    store = HourlyHistoryStore(tmp_path / "cache", seed_path=(DEPLOY / "b2_retune_history_seed.json") if seed else None,
                               candle_pause_s=0, funding_page_pause_s=0, sleep=_no_sleep)
    eng = B2RetuneEngine(session_factory=sf, client=client, strategy_id=sid, clock=lambda: clock["now"],
                         history_store=store)
    assert p.coins == ("BTC", "ETH", "SOL", "AVAX")
    return eng, clock


async def _rows(sf, sid):
    async with sf() as s:
        eq = (await s.execute(select(B2Equity).where(B2Equity.strategy_id == sid)
                              .order_by(B2Equity.ts_ms, B2Equity.coin))).scalars().all()
        ev = (await s.execute(select(B2Event).where(B2Event.strategy_id == sid)
                              .order_by(B2Event.ts_ms, B2Event.id))).scalars().all()
    return eq, ev


NOW0 = SEED_END + 30 * HOUR_MS + 90_000                      # last closed bar opens 29 h after the seed's end


@pytest.mark.asyncio
async def test_first_tick_replays_the_history_and_marks_it_backfilled(sf, tmp_path):
    sid = await _add_strategy(sf)
    eng, _ = _engine(sf, sid, FakeClient(), NOW0, tmp_path)
    r = await eng.tick()
    launch = (NOW0 // HOUR_MS) * HOUR_MS
    assert r["launch"] and r["chosen"] == "b2"
    state = await B2RetuneRepo(sf).get_state(sid)
    assert state["launch_ms"] == launch and state["refits"] == 7

    eq, ev = await _rows(sf, sid)
    # equity exists from the first choice (the 2026-06-01 00:00 candle, decided at its close) to the launch hour;
    # the first row, stamped with that candle's open, is the capital
    ts = sorted({e.ts_ms for e in eq})
    assert ts[0] == 1_780_272_000_000 and ts[-1] == launch and all(e.ts_ms <= launch for e in eq)
    assert len(eq) == 4 * len(ts)
    retunes = [e for e in ev if e.kind == "retune"]
    assert [e.details_json["choice"] for e in retunes] == [None, None, "b2_cold", "b2_cold", "b2_cold", "b2_cold", "b2"]
    assert all(e.details_json["phase"] == "backfill" for e in ev)
    assert sum(1 for e in ev if e.kind == "backfill") == 1
    assert retunes[2].details_json["first_choice"] is True and state["first_choice_ms"] == ts[1]
    assert sum(e.equity for e in eq if e.ts_ms == ts[0]) == pytest.approx(257.0)
    # the first move is the cost of opening the positions
    assert 256.0 < sum(e.equity for e in eq if e.ts_ms == ts[1]) < 257.0


@pytest.mark.asyncio
async def test_live_ticks_continue_and_are_not_backfill(sf, tmp_path):
    sid = await _add_strategy(sf)
    eng, clock = _engine(sf, sid, FakeClient(), NOW0, tmp_path)
    await eng.tick()
    launch = (NOW0 // HOUR_MS) * HOUR_MS
    clock["now"] = NOW0 + 5 * HOUR_MS
    r = await eng.tick()
    assert r["hours"] == 5 and not r["launch"]
    eq, ev = await _rows(sf, sid)
    live = [e for e in eq if e.ts_ms > launch]
    assert sorted({e.ts_ms for e in live}) == [launch + k * HOUR_MS for k in range(1, 6)]
    assert (await B2RetuneRepo(sf).get_state(sid))["launch_ms"] == launch       # unchanged by later ticks
    assert (await eng.tick())["hours"] == 0                                     # same hour again: nothing new


@pytest.mark.asyncio
async def test_a_restart_resumes_from_the_saved_state_and_equals_one_continuous_run(sf, tmp_path):
    sid = await _add_strategy(sf)
    eng, clock = _engine(sf, sid, FakeClient(), NOW0, tmp_path / "a")
    await eng.tick()
    clock["now"] = NOW0 + 9 * HOUR_MS
    await eng.tick()
    # a second engine object (a restart): fresh store, same cache directory, same database
    eng2, clock2 = _engine(sf, sid, FakeClient(), NOW0 + 20 * HOUR_MS, tmp_path / "a")
    assert (await eng2.tick())["hours"] == 11

    eq, _ = await _rows(sf, sid)
    # the same history in one go, on another database
    eng_b = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng_b)
    sf_b = make_session_factory(eng_b)
    sid_b = await _add_strategy(sf_b)
    one, _ = _engine(sf_b, sid_b, FakeClient(), NOW0 + 20 * HOUR_MS, tmp_path / "b")
    await one.tick()
    eq_b, _ = await _rows(sf_b, sid_b)
    await eng_b.dispose()
    assert [(e.ts_ms, e.coin) for e in eq] == [(e.ts_ms, e.coin) for e in eq_b]
    assert max(abs(a.equity - b.equity) for a, b in zip(eq, eq_b)) < 1e-9


@pytest.mark.asyncio
async def test_history_is_extended_incrementally_never_refetched(sf, tmp_path):
    sid = await _add_strategy(sf)
    client = FakeClient()
    eng, clock = _engine(sf, sid, client, NOW0, tmp_path)
    await eng.tick()
    first_calls = list(client.candle_calls)
    # the seed covers everything up to its end; only the tail after it (minus the integrity re-read) is asked for
    assert {c for c, *_ in first_calls} == {"BTC", "ETH", "SOL", "AVAX"}
    assert all(start >= SEED_END - 6 * HOUR_MS for _, start, _ in first_calls)
    clock["now"] = NOW0 + 3 * HOUR_MS
    client.candle_calls.clear()
    await eng.tick()
    assert len(client.candle_calls) == 4 and all(start >= NOW0 - 7 * HOUR_MS for _, start, _ in client.candle_calls)
    # the cache file holds seed + extension, contiguous, ending at the newest closed candle
    cache = tmp_path / "cache" / "hourly" / "BTC.json"
    entry = json.loads(cache.read_text())
    opens = [r[0] for r in entry["candles"]]
    assert opens[0] == SEED["origin_ms"] and opens[-1] == SEED_END + 32 * HOUR_MS
    assert opens == list(range(opens[0], opens[-1] + HOUR_MS, HOUR_MS))


@pytest.mark.asyncio
async def test_the_cache_is_not_purged_by_age(sf, tmp_path):
    """Unlike the trend store (14 days), hours older than ~5000 cannot be fetched again, so nothing expires."""
    sid = await _add_strategy(sf)
    eng, _ = _engine(sf, sid, FakeClient(), NOW0, tmp_path)
    await eng.tick()
    files = list((tmp_path / "cache" / "hourly").glob("*.json"))
    assert len(files) == 4
    for f in files:
        os.utime(f, (1_000_000_000, 1_000_000_000))                    # 2001
    HourlyHistoryStore(tmp_path / "cache")                              # opening a store must not delete them
    assert all(f.is_file() for f in files)


@pytest.mark.asyncio
async def test_a_paused_strategy_does_nothing(sf, tmp_path):
    sid = await _add_strategy(sf, status="paused")
    client = FakeClient()
    eng, _ = _engine(sf, sid, client, NOW0, tmp_path)
    assert (await eng.tick())["hours"] == 0 and not client.candle_calls
    assert await B2RetuneRepo(sf).get_state(sid) is None


@pytest.mark.asyncio
async def test_changed_params_refuse_to_continue_a_saved_state(sf, tmp_path):
    sid = await _add_strategy(sf)
    eng, clock = _engine(sf, sid, FakeClient(), NOW0, tmp_path)
    await eng.tick()
    async with session_scope(sf) as s:
        row = await s.get(Strategy, sid)
        changed = dict(row.params_json)
        changed["retune_candidates"] = {"b2": {"hedge_margin_headroom": 1.0}, "b2_cold": {"hedge_margin_headroom": 1.4}}
        row.params_json = changed
    clock["now"] = NOW0 + 2 * HOUR_MS
    with pytest.raises(ValueError, match="params changed"):
        await eng.tick()


@pytest.mark.asyncio
async def test_without_the_seed_the_missing_history_is_an_error_not_a_silent_start(sf, tmp_path):
    sid = await _add_strategy(sf)
    eng, _ = _engine(sf, sid, FakeClient(), NOW0, tmp_path, seed=False)
    # the fake client serves everything, so give the cache a hole instead: a coin with no data before the seed end
    store = eng._history(B2RetuneParams.from_dict(PARAMS))
    store._store("hourly", "BTC", {"coin": "BTC", "candles_to_ms": SEED_END, "funding_to_ms": SEED_END,
                                    "candles": [[SEED["origin_ms"] + HOUR_MS * 5, 1.0, 1.0]], "funding": []})
    with pytest.raises(Exception, match="no candle|later ones exist"):
        await eng.tick()
    assert await B2RetuneRepo(sf).get_state(sid) is None


@pytest.mark.asyncio
async def test_the_state_row_is_invisible_to_the_plain_b2_readers(sf, tmp_path):
    sid = await _add_strategy(sf)
    eng, _ = _engine(sf, sid, FakeClient(), NOW0, tmp_path)
    await eng.tick()
    repo = B2Repo(sf)
    books = await repo.books(sid)
    assert sorted(b.coin for b in books) == ["AVAX", "BTC", "ETH", "SOL"]
    assert await repo.get_book(sid, RETUNE_STATE_COIN) is None
    async with sf() as s:
        rows = (await s.execute(select(B2Book.coin).where(B2Book.strategy_id == sid))).scalars().all()
    assert sorted(rows) == ["AVAX", "BTC", "ETH", "SOL", RETUNE_STATE_COIN]
    # the published coin books add up to the book equity
    state = RetuneState.from_dict(await B2RetuneRepo(sf).get_state(sid), B2RetuneParams.from_dict(PARAMS))
    latest = {e.coin: e.equity for e in await repo.latest_equity(sid)}
    assert sum(latest.values()) == pytest.approx(state.book_equity, abs=1e-9)
