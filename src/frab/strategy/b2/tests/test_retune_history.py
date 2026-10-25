"""Hourly history store of b2_retune: seed, incremental extension, integrity re-read, steppable bars."""
from __future__ import annotations

import json
import math
import os

import pytest

from frab.exchanges.hyperliquid.wire import HLCandle, HLFundingRecord
from frab.strategy.b2.retune_history import HOUR_MS, CoinSeries, HistoryGap, HourlyHistoryStore

T0 = 1_780_000_000_000 - 1_780_000_000_000 % HOUR_MS


async def _nosleep(_s):
    return None


class Client:
    def __init__(self, last_open: int, *, close=lambda t: 100.0 + (t - T0) // HOUR_MS, funding_every=1,
                 skip_candles: set[int] | None = None):
        self.last_open = last_open
        self.close = close
        self.candles: list[tuple[int, int]] = []
        self.funding: list[tuple[int, int | None]] = []
        self.funding_every = funding_every
        self.skip = skip_candles or set()

    async def candle_snapshot(self, coin, interval, start_ms, end_ms):
        self.candles.append((start_ms, end_ms))
        return [HLCandle(coin=coin, open_ms=t, close_ms=t + HOUR_MS, close=self.close(t), high=self.close(t) + 1)
                for t in range(start_ms - start_ms % HOUR_MS, min(end_ms, self.last_open + HOUR_MS * 2) + 1, HOUR_MS)
                if t not in self.skip and t >= T0]

    async def funding_history(self, coin, since_ms, *, end_ms=None, page_pause_s=0.0):
        self.funding.append((since_ms, end_ms))
        return [HLFundingRecord(coin=coin, ts_ms=t + 3, rate=0.00001 * ((t - T0) // HOUR_MS % 5), premium=0.0)
                for t in range(since_ms - since_ms % HOUR_MS, (end_ms if end_ms is not None else since_ms) + 1, HOUR_MS)
                if t >= T0 and (t - T0) // HOUR_MS % self.funding_every == 0]


def write_seed(path, hours=20, coin="BTC"):
    seed = {"origin_ms": T0, "last_open_ms": T0 + (hours - 1) * HOUR_MS,
            "coins": {coin: {"close": [100.0 + i for i in range(hours)], "high": [101.0 + i for i in range(hours)],
                             "funding": [0.00001 * (i % 5) for i in range(hours)]}}}
    path.write_text(json.dumps(seed))


@pytest.mark.asyncio
async def test_first_extend_copies_the_seed_into_the_cache_and_asks_only_for_what_follows(tmp_path):
    write_seed(tmp_path / "seed.json")
    store = HourlyHistoryStore(tmp_path / "c", seed_path=tmp_path / "seed.json", candle_pause_s=0,
                               funding_page_pause_s=0, sleep=_nosleep)
    client = Client(T0 + 30 * HOUR_MS)
    await store.extend(client, "BTC", T0 + 24 * HOUR_MS, T0)
    assert client.candles[0][0] == T0 + 20 * HOUR_MS - 6 * HOUR_MS             # seed end minus the integrity re-read
    s = store.series("BTC", T0, T0 + 24 * HOUR_MS)
    assert s.close == [100.0 + i for i in range(25)]                           # seed rows kept, fetched ones appended
    assert s.high[24] == 125.0 and s.funding[24] == pytest.approx(0.00004)
    assert (tmp_path / "c" / "hourly" / "BTC.json").is_file()


@pytest.mark.asyncio
async def test_a_second_extend_fetches_only_the_new_tail_and_a_covered_request_fetches_nothing(tmp_path):
    write_seed(tmp_path / "seed.json")
    store = HourlyHistoryStore(tmp_path / "c", seed_path=tmp_path / "seed.json", candle_pause_s=0,
                               funding_page_pause_s=0, sleep=_nosleep)
    client = Client(T0 + 40 * HOUR_MS)
    await store.extend(client, "BTC", T0 + 24 * HOUR_MS, T0)
    n = len(client.candles)
    await store.extend(client, "BTC", T0 + 24 * HOUR_MS, T0)                   # already complete
    assert len(client.candles) == n
    await store.extend(client, "BTC", T0 + 27 * HOUR_MS, T0)
    assert client.candles[-1][0] == T0 + 25 * HOUR_MS - 6 * HOUR_MS
    # a fresh store object on the same directory (a restart) continues from the file, with no seed needed
    store2 = HourlyHistoryStore(tmp_path / "c", candle_pause_s=0, funding_page_pause_s=0, sleep=_nosleep)
    client2 = Client(T0 + 40 * HOUR_MS)
    await store2.extend(client2, "BTC", T0 + 28 * HOUR_MS, T0)
    assert client2.candles[0][0] == T0 + 28 * HOUR_MS - 6 * HOUR_MS
    assert store2.series("BTC", T0, T0 + 28 * HOUR_MS).close == [100.0 + i for i in range(29)]


@pytest.mark.asyncio
async def test_an_open_candle_is_never_stored_and_a_changed_old_candle_keeps_the_stored_row(tmp_path):
    write_seed(tmp_path / "seed.json")
    store = HourlyHistoryStore(tmp_path / "c", seed_path=tmp_path / "seed.json", candle_pause_s=0,
                               funding_page_pause_s=0, sleep=_nosleep)
    client = Client(T0 + 40 * HOUR_MS, close=lambda t: 100.0 + (t - T0) // HOUR_MS + (50.0 if t == T0 + 18 * HOUR_MS else 0.0))
    await store.extend(client, "BTC", T0 + 21 * HOUR_MS, T0)                   # the client also returns open hours 22, 23
    s = store.series("BTC", T0, T0 + 40 * HOUR_MS)
    assert math.isnan(s.close[22])
    assert s.close[18] == 118.0 and store.mismatches == 1                      # the re-read differs: stored wins


def test_the_ready_bar_waits_for_the_newest_funding_but_zero_fills_an_older_missing_one():
    n = 6
    nan = math.nan
    s = CoinSeries("BTC", T0, [1.0] * n, [1.0] * n, [0.1, nan, 0.1, 0.1, nan, nan])
    assert s.ready_through_ms() == T0 + 3 * HOUR_MS                            # hour 1 is skipped by HL (later ones exist)
    s2 = CoinSeries("BTC", T0, [1.0] * 4 + [nan, nan], [1.0] * 4 + [nan, nan], [0.1] * 6)
    assert s2.ready_through_ms() == T0 + 3 * HOUR_MS                           # candles not there yet
    assert CoinSeries("BTC", T0, [1.0], [1.0], [nan]).ready_through_ms() is None


def test_a_hole_in_the_middle_of_the_history_is_an_error():
    nan = math.nan
    s = CoinSeries("BTC", T0, [1.0, nan, 1.0], [1.0, nan, 1.0], [0.1] * 3)
    with pytest.raises(HistoryGap, match="later ones exist"):
        s.ready_through_ms()


@pytest.mark.asyncio
async def test_without_a_seed_a_long_gap_is_paged_forward(tmp_path):
    store = HourlyHistoryStore(tmp_path / "c", candle_pause_s=0, funding_page_pause_s=0, sleep=_nosleep)
    client = Client(T0 + 12000 * HOUR_MS)
    await store.extend(client, "ETH", T0 + 9000 * HOUR_MS, T0)
    assert len(client.candles) >= 2                                             # more than one 4900-hour page
    s = store.series("ETH", T0, T0 + 9000 * HOUR_MS)
    assert not any(math.isnan(c) for c in s.close)


def test_opening_a_store_does_not_purge_old_files(tmp_path):
    f = tmp_path / "c" / "hourly" / "BTC.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"coin": "BTC", "candles_to_ms": 0, "funding_to_ms": 0, "candles": [], "funding": []}))
    os.utime(f, (1_000_000_000, 1_000_000_000))
    HourlyHistoryStore(tmp_path / "c")
    assert f.is_file()
