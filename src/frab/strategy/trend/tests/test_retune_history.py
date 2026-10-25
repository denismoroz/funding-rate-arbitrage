"""The backfill's fetched-history store: cached on disk, topped up not refetched, polite, resumable."""
from __future__ import annotations

import os
import time

import pytest

from frab.exchanges.hyperliquid.wire import HLCandle, HLFundingRecord
from frab.strategy.trend.retune_history import CACHE_TTL_DAYS, DAY_MS, HOUR_MS, HistoryStore

D0 = 1_767_571_200_000


class Client:
    def __init__(self):
        self.candle_calls: list[tuple[str, int, int]] = []
        self.funding_calls: list[tuple[str, int, float]] = []
        self.fail = False

    async def candle_snapshot(self, coin, interval, start_ms, end_ms):
        self.candle_calls.append((coin, start_ms, end_ms))
        if self.fail:
            raise ConnectionError("down")
        return [HLCandle(coin=coin, open_ms=t, close_ms=t + DAY_MS - 1, close=100.0 + (t - D0) / DAY_MS,
                         volume=5.0) for t in range(start_ms - start_ms % DAY_MS, end_ms + 1, DAY_MS)
                if t <= end_ms]

    async def funding_history(self, coin, since_ms, *, end_ms=None, page_pause_s=0.0):
        self.funding_calls.append((coin, since_ms, page_pause_s))
        if self.fail:
            raise ConnectionError("down")
        until = D0 + 40 * DAY_MS if end_ms is None else end_ms
        return [HLFundingRecord(coin=coin, ts_ms=t + 20, rate=1e-5, premium=0.0)
                for t in range(since_ms - since_ms % HOUR_MS, until, HOUR_MS)]


SLEPT: list[float] = []


async def fake_sleep(seconds: float) -> None:
    SLEPT.append(seconds)


def store(tmp_path, **kw) -> HistoryStore:
    return HistoryStore(tmp_path / "c", candle_pause_s=kw.pop("candle", 0.0),
                        funding_page_pause_s=kw.pop("page", 0.0), sleep=fake_sleep, **kw)


@pytest.mark.asyncio
async def test_daily_candles_are_fetched_once_then_served_from_memory_and_from_disk(tmp_path):
    c, s = Client(), store(tmp_path)
    first = await s.daily(c, "BTC", D0, D0 + 10 * DAY_MS)
    assert len(c.candle_calls) == 1 and len(first) == 11 and first[D0] == (100.0, 5.0)
    assert await s.daily(c, "BTC", D0, D0 + 10 * DAY_MS) == first and len(c.candle_calls) == 1
    s2 = store(tmp_path)                                         # a new process finds it on disk
    assert await s2.daily(c, "BTC", D0 + 2 * DAY_MS, D0 + 10 * DAY_MS) == {
        o: v for o, v in first.items() if o >= D0 + 2 * DAY_MS}
    assert len(c.candle_calls) == 1 and s2.stats.cache_hits == 1


@pytest.mark.asyncio
async def test_a_later_need_fetches_only_the_tail_and_replaces_the_candle_that_was_still_open(tmp_path):
    c, s = Client(), store(tmp_path)
    await s.daily(c, "BTC", D0, D0 + 10 * DAY_MS)
    again = await s.daily(c, "BTC", D0, D0 + 14 * DAY_MS)
    assert c.candle_calls[1][1] == D0 + 7 * DAY_MS               # re-reads the last 3 days, not the 14
    assert sorted(again) == [D0 + k * DAY_MS for k in range(15)]
    # the "complete through" argument decides: a request that needs less than what is cached is a hit
    await s.daily(c, "BTC", D0, D0 + 20 * DAY_MS, complete_through_ms=D0 + 14 * DAY_MS)
    assert len(c.candle_calls) == 2


@pytest.mark.asyncio
async def test_an_earlier_start_refetches_from_that_start(tmp_path):
    c, s = Client(), store(tmp_path)
    await s.daily(c, "BTC", D0, D0 + 10 * DAY_MS)
    got = await s.daily(c, "BTC", D0 - 5 * DAY_MS, D0 + 10 * DAY_MS)
    assert c.candle_calls[1][1] == D0 - 5 * DAY_MS and min(got) == D0 - 5 * DAY_MS and len(got) == 16


@pytest.mark.asyncio
async def test_a_failed_fetch_raises_and_leaves_what_was_stored(tmp_path):
    c, s = Client(), store(tmp_path)
    await s.daily(c, "BTC", D0, D0 + 10 * DAY_MS)
    c.fail = True
    with pytest.raises(RuntimeError, match="BTC: daily fetch failed"):
        await s.daily(c, "BTC", D0, D0 + 20 * DAY_MS)
    with pytest.raises(RuntimeError, match="ETH: daily fetch failed"):
        await s.daily(c, "ETH", D0, D0 + 20 * DAY_MS)
    assert sorted(p.stem for p in (tmp_path / "c" / "daily").glob("*.json")) == ["BTC"]
    c.fail = False
    assert len(await store(tmp_path).daily(c, "BTC", D0, D0 + 10 * DAY_MS)) == 11    # still served from disk


@pytest.mark.asyncio
async def test_funding_is_cached_paged_politely_and_topped_up(tmp_path):
    c, s = Client(), store(tmp_path, page=2.7, candle=2.0)
    SLEPT.clear()
    rows = await s.funding(c, "BTC", D0, D0 + 5 * DAY_MS)
    assert c.funding_calls == [("BTC", D0, 2.7)] and rows[0][0] == D0 + 20
    await s.daily(c, "BTC", D0, D0 + 5 * DAY_MS)
    assert SLEPT == [2.7, 2.0]                                           # a pause after every network call
    assert await s.funding(c, "BTC", D0 + DAY_MS, D0 + 5 * DAY_MS) == [r for r in rows if r[0] >= D0 + DAY_MS]
    assert len(c.funding_calls) == 1
    await s.funding(c, "BTC", D0, D0 + 9 * DAY_MS)                       # needs more: top up from just before
    assert c.funding_calls[1][1] == D0 + 5 * DAY_MS - 6 * HOUR_MS
    stored = await store(tmp_path).funding(c, "BTC", D0, D0 + 9 * DAY_MS)
    assert len(c.funding_calls) == 2 and len({t for t, _ in stored}) == len(stored)     # no duplicate records


@pytest.mark.asyncio
async def test_a_corrupt_cache_file_is_refetched_not_fatal(tmp_path):
    c, s = Client(), store(tmp_path)
    await s.daily(c, "BTC", D0, D0 + 3 * DAY_MS)
    (tmp_path / "c" / "daily" / "BTC.json").write_text("{not json")
    assert len(await store(tmp_path).daily(c, "BTC", D0, D0 + 3 * DAY_MS)) == 4 and len(c.candle_calls) == 2


@pytest.mark.asyncio
async def test_release_drops_memory_only_and_old_files_are_purged(tmp_path):
    c, s = Client(), store(tmp_path)
    await s.daily(c, "BTC", D0, D0 + 3 * DAY_MS)
    s.release()
    assert len(await s.daily(c, "BTC", D0, D0 + 3 * DAY_MS)) == 4 and len(c.candle_calls) == 1
    path = tmp_path / "c" / "daily" / "BTC.json"
    old = time.time() - (CACHE_TTL_DAYS + 1) * 86_400
    os.utime(path, (old, old))
    store(tmp_path)
    assert not path.exists()


@pytest.mark.asyncio
async def test_hip3_coin_names_make_a_safe_file_name(tmp_path):
    c, s = Client(), store(tmp_path)
    await s.daily(c, "xyz:AAPL", D0, D0 + DAY_MS)
    assert [p.name for p in (tmp_path / "c" / "daily").glob("*.json")] == ["xyz_AAPL.json"]
