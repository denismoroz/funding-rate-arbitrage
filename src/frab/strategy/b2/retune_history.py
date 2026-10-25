"""Hourly history of the `b2_retune` book: candles with highs and funding, cached on disk and extended.

The book is replayed from the start of its history (March 2026) and keeps growing as an expanding window, but
Hyperliquid's `candleSnapshot` serves only the most recent ~5000 hourly candles, so the older hours can never be
fetched again. Hence:

* **Seed.** `deploy/b2_retune_history_seed.json` holds the hours Hyperliquid no longer serves, exported from the
  q-lab stand's snapshot (`scripts/export_b2_retune_seed.py`) so the replay starts at the same hour as the
  stand. The first request for a coin copies the seed into its cache file and fetches only what follows it.
* **Cache.** One file per coin, `<cache_dir>/hourly/<COIN>.json`, written atomically after every extension. Unlike
  the trend store it is **never purged by age**: deleting an entry loses hours for good. The `purge_old` switch
  of the shared `HistoryStore` is off here.
* **Incremental.** An entry is complete up to `candles_to_ms` / `funding_to_ms`; only the tail is requested,
  re-reading the last few hours as an integrity check. A closed candle never changes, so on a difference the
  stored row wins and the mismatch is counted and logged.

A bar is *steppable* when its close and high are known and funding is known for it or for a later hour (an hour
HL skipped reads as 0, exactly like the live engine's `funding.get(h, 0.0)`; the newest hour is waited for
instead, so a funding record that is merely late is not stored as 0). A hole in the middle of the history is an
error, not something to wait for.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from frab.strategy.trend.retune_history import HistoryStore

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
KIND = "hourly"
REREAD_MS = 6 * HOUR_MS               # the tail re-read as an integrity check (closed candles do not change)
CANDLE_PAGE_HOURS = 4_900             # HL answers at most 5000 candles per request


def floor_hour(ms: int) -> int:
    return ms - ms % HOUR_MS


class HistoryGap(RuntimeError):
    """The cached history has a hole before its newest hour: the book cannot be replayed through it."""


@dataclass
class CoinSeries:
    """Closes, highs and funding of one coin on the hourly grid starting at `origin_ms` (NaN = unknown)."""
    coin: str
    origin_ms: int
    close: list[float]
    high: list[float]
    funding: list[float]

    def __len__(self) -> int:
        return len(self.close)

    def index(self, ms: int) -> int:
        return (ms - self.origin_ms) // HOUR_MS

    def ready_through_ms(self) -> int | None:
        """Open time of the newest steppable bar, or None when not even the first one is."""
        n = len(self.close)
        known = [not (math.isnan(self.close[i]) or math.isnan(self.high[i])) for i in range(n)]
        first_hole = next((i for i, ok in enumerate(known) if not ok), n)
        if any(known[first_hole:]):
            raise HistoryGap(f"{self.coin}: no candle for {self.origin_ms + first_hole * HOUR_MS} but later ones exist")
        last_funding = max((i for i, f in enumerate(self.funding) if not math.isnan(f)), default=-1)
        last = min(first_hole, last_funding + 1) - 1
        return None if last < 0 else self.origin_ms + last * HOUR_MS


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


class HourlyHistoryStore(HistoryStore):
    def __init__(self, cache_dir: str | Path | None = None, *, seed_path: str | Path | None = None,
                 candle_pause_s: float = 2.0, funding_page_pause_s: float = 2.7,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        super().__init__(cache_dir, candle_pause_s=candle_pause_s, funding_page_pause_s=funding_page_pause_s,
                         sleep=sleep, purge_old=False)
        self.seed_path = Path(seed_path) if seed_path else None
        self._seed: dict | None = None
        self.mismatches = 0

    # ── seed ─────────────────────────────────────────────────────────────
    def _seed_entry(self, coin: str) -> dict | None:
        if self.seed_path is None:
            return None
        if self._seed is None:
            try:
                self._seed = json.loads(self.seed_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"history seed {self.seed_path} cannot be read ({exc}); "
                                   "deploy the file or the pre-3/30 hours are lost") from exc
        cols = self._seed["coins"].get(coin)
        if cols is None:
            return None
        origin, step = int(self._seed["origin_ms"]), HOUR_MS
        n = len(cols["close"])
        end = origin + n * step
        return {"coin": coin, "candles_to_ms": end, "funding_to_ms": end,
                "candles": [[origin + i * step, cols["close"][i], cols["high"][i]] for i in range(n)
                            if cols["close"][i] is not None and cols["high"][i] is not None],
                "funding": [[origin + i * step, cols["funding"][i]] for i in range(n)
                            if cols["funding"][i] is not None]}

    def _entry(self, coin: str) -> dict:
        entry = self._load(KIND, coin) or self._seed_entry(coin)
        return entry or {"coin": coin, "candles_to_ms": 0, "funding_to_ms": 0, "candles": [], "funding": []}

    # ── extension ────────────────────────────────────────────────────────
    async def extend(self, client, coin: str, last_closed_ms: int, origin_ms: int) -> None:
        """Make the entry of `coin` complete through the candle opening at `last_closed_ms`."""
        need_to = last_closed_ms + HOUR_MS
        entry = self._entry(coin)
        if entry["candles_to_ms"] >= need_to and entry["funding_to_ms"] >= need_to:
            self.stats.cache_hits += 1
            if self._load(KIND, coin) is None:                   # a seed that already covers the request
                self._store(KIND, coin, entry)
            return
        candles = {int(r[0]): [r[1], r[2]] for r in entry["candles"]}
        funding = {int(r[0]): r[1] for r in entry["funding"]}

        if entry["candles_to_ms"] < need_to:
            start = max(origin_ms, entry["candles_to_ms"] - REREAD_MS) if candles else origin_ms
            while start < need_to:
                try:
                    batch = await client.candle_snapshot(coin, "1h", start, min(need_to, start + CANDLE_PAGE_HOURS * HOUR_MS))
                except Exception as exc:
                    raise RuntimeError(f"{coin}: hourly candle fetch failed ({type(exc).__name__}); tick deferred") from exc
                self.stats.candle_requests += 1
                fresh = [c for c in batch if c.open_ms < need_to and c.high is not None]
                for c in fresh:
                    old = candles.get(c.open_ms)
                    if old is None:
                        candles[c.open_ms] = [c.close, c.high]
                    elif not (_same(old[0], c.close) and _same(old[1], c.high)):
                        self.mismatches += 1
                        logger.warning("b2 retune %s: candle %d differs from the stored one (%s vs %s); "
                                       "the stored one stays", coin, c.open_ms, old, [c.close, c.high])
                if self.candle_pause_s:
                    await self._sleep(self.candle_pause_s)
                if not fresh:
                    break
                nxt = max(c.open_ms for c in fresh) + HOUR_MS
                if nxt <= start:
                    break
                start = nxt
            entry["candles_to_ms"] = need_to

        if entry["funding_to_ms"] < need_to:
            since = max(origin_ms, entry["funding_to_ms"] - REREAD_MS) if funding else origin_ms
            try:
                records = await client.funding_history(coin, since, end_ms=need_to - 1,
                                                       page_pause_s=self.funding_page_pause_s)
            except Exception as exc:
                raise RuntimeError(f"{coin}: hourly funding fetch failed ({type(exc).__name__}); tick deferred") from exc
            self.stats.funding_coins += 1
            for r in records:
                hour = floor_hour(r.ts_ms)
                if hour >= need_to:
                    continue
                stored = funding.get(hour)
                if stored is None:
                    funding[hour] = r.rate
                elif not _same(stored, r.rate):
                    self.mismatches += 1
                    logger.warning("b2 retune %s: funding of %d differs from the stored one (%s vs %s); "
                                   "the stored one stays", coin, hour, stored, r.rate)
            entry["funding_to_ms"] = need_to

        entry["candles"] = [[o, v[0], v[1]] for o, v in sorted(candles.items())]
        entry["funding"] = [[o, v] for o, v in sorted(funding.items())]
        self._store(KIND, coin, entry)
        if self.funding_page_pause_s:
            await self._sleep(self.funding_page_pause_s)

    # ── reading ──────────────────────────────────────────────────────────
    def series(self, coin: str, origin_ms: int, last_closed_ms: int) -> CoinSeries:
        n = (last_closed_ms - origin_ms) // HOUR_MS + 1
        close = [math.nan] * n
        high = [math.nan] * n
        fund = [math.nan] * n
        entry = self._entry(coin)
        for o, c, h in entry["candles"]:
            i = (o - origin_ms) // HOUR_MS
            if 0 <= i < n:
                close[i], high[i] = c, h
        for o, r in entry["funding"]:
            i = (o - origin_ms) // HOUR_MS
            if 0 <= i < n:
                fund[i] = r
        return CoinSeries(coin, origin_ms, close, high, fund)
