"""Fetched Hyperliquid history for the backfill: polite, cached on disk, resumable (T52b).

A backfill pulls ~230 coins of daily candles back to the spec's window start and the funding history of every
coin a candidate may hold -- thousands of requests. Two things follow:

* every coin is stored the moment it is fetched (`<cache_dir>/{daily,funding}/<coin>.json`, written
  atomically), so a crash, a restart or a failed request costs at most the coin in flight and the next tick
  continues where the last one stopped;
* the cache is shared: several `trend_retune*` books use the same store, the second one finds everything
  already there.

A cached entry is complete up to `fetched_to_ms`, the end of the window it was asked for. It answers a
request when it starts no later than the request and is complete through what the caller needs
(`complete_through_ms`); otherwise only the missing tail is fetched, re-reading the last few days because the
newest daily candle was still open when it was stored.

Nothing here knows what a "bar" is; the point-in-time cut happens later, in `retune.build_day` /
`retune_backfill.IndexedHistory`, which only ever look at data dated before the bar.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
TAIL_REREAD_MS = 3 * DAY_MS          # the newest candle of an older fetch may have been partial
FUNDING_REREAD_MS = 6 * HOUR_MS
CACHE_TTL_DAYS = 14                  # entries older than this are purged when a store is created

Daily = dict[int, tuple[float, float | None]]          # open_ms -> (close, base volume)
Funding = list[tuple[int, float]]                      # (ts_ms, rate), sorted by ts


@dataclass
class FetchStats:
    candle_requests: int = 0
    funding_coins: int = 0
    cache_hits: int = 0


class HistoryStore:
    def __init__(self, cache_dir: str | Path | None = None, *, candle_pause_s: float = 2.0,
                 funding_page_pause_s: float = 2.7,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.dir = Path(cache_dir) if cache_dir else None
        self.candle_pause_s = candle_pause_s
        self.funding_page_pause_s = funding_page_pause_s
        self._sleep = sleep
        self._mem: dict[tuple[str, str], dict] = {}
        self.lock = asyncio.Lock()               # one backfill fetches at a time; the next finds it cached
        self.stats = FetchStats()
        if self.dir:
            self._purge_old()

    def release(self) -> None:
        """Drop the in-memory copy (a few hundred MB for a full backfill); the disk cache stays."""
        self._mem.clear()

    # ── disk ─────────────────────────────────────────────────────────────
    @staticmethod
    def _file_name(coin: str) -> str:
        return coin.replace("/", "_").replace(":", "_") + ".json"       # HIP-3 names carry a colon

    def _path(self, kind: str, coin: str) -> Path | None:
        return self.dir / kind / self._file_name(coin) if self.dir else None

    def _purge_old(self) -> None:
        cutoff = time.time() - CACHE_TTL_DAYS * 86_400
        for path in (self.dir or Path()).glob("*/*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def _load(self, kind: str, coin: str) -> dict | None:
        key = (kind, coin)
        if key in self._mem:
            return self._mem[key]
        path = self._path(kind, coin)
        if path is not None and path.is_file():
            try:
                self._mem[key] = json.loads(path.read_text(encoding="utf-8"))
                return self._mem[key]
            except (OSError, ValueError):
                logger.warning("history cache %s unreadable; refetching", path)
        return None

    def _store(self, kind: str, coin: str, entry: dict) -> None:
        self._mem[(kind, coin)] = entry
        path = self._path(kind, coin)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)

    # ── daily candles ────────────────────────────────────────────────────
    def peek_daily(self, coin: str, start_ms: int, complete_through_ms: int) -> Daily | None:
        """The cached candles of `coin` from `start_ms` if the entry covers the request, else None."""
        entry = self._load("daily", coin)
        if entry and entry["start_ms"] <= start_ms and entry["fetched_to_ms"] >= complete_through_ms:
            return {int(o): (c, v) for o, c, v in entry["rows"] if o >= start_ms}
        return None

    async def daily(self, client, coin: str, start_ms: int, end_ms: int,
                    complete_through_ms: int | None = None) -> Daily:
        """Daily candles of `coin` opening in [start_ms, end_ms], from the cache or fetched (and cached)."""
        need = end_ms if complete_through_ms is None else complete_through_ms
        hit = self.peek_daily(coin, start_ms, need)
        if hit is not None:
            self.stats.cache_hits += 1
            return hit
        entry = self._load("daily", coin)
        rows: dict[int, list] = {}
        fetch_from = keep_start = start_ms
        if entry and entry["start_ms"] <= start_ms:
            rows = {int(r[0]): r for r in entry["rows"]}
            fetch_from = max(start_ms, entry["fetched_to_ms"] - TAIL_REREAD_MS)
            keep_start = entry["start_ms"]
        try:
            candles = await client.candle_snapshot(coin, "1d", fetch_from, end_ms)
        except Exception as exc:
            raise RuntimeError(f"{coin}: daily fetch failed ({type(exc).__name__}); backfill deferred") from exc
        self.stats.candle_requests += 1
        for c in candles:
            rows[c.open_ms] = [c.open_ms, c.close, getattr(c, "volume", None)]
        merged = [rows[o] for o in sorted(rows)]
        self._store("daily", coin, {"coin": coin, "start_ms": keep_start, "fetched_to_ms": end_ms,
                                    "rows": merged})
        if self.candle_pause_s:
            await self._sleep(self.candle_pause_s)
        return {o: (r[1], r[2]) for o, r in rows.items() if o >= start_ms}

    # ── funding ──────────────────────────────────────────────────────────
    def peek_funding(self, coin: str, since_ms: int, complete_through_ms: int) -> Funding | None:
        entry = self._load("funding", coin)
        if entry and entry["since_ms"] <= since_ms and entry["fetched_to_ms"] >= complete_through_ms:
            return [(int(t), r) for t, r in entry["rows"] if t >= since_ms]
        return None

    async def funding(self, client, coin: str, since_ms: int, end_ms: int,
                      complete_through_ms: int | None = None) -> Funding:
        """Funding records of `coin` in [since_ms, end_ms], from the cache or fetched page by page. Only the
        window a caller needs is fetched: a coin that was in a universe for three weeks costs three weeks."""
        need = end_ms if complete_through_ms is None else complete_through_ms
        hit = self.peek_funding(coin, since_ms, need)
        if hit is not None:
            self.stats.cache_hits += 1
            return hit
        entry = self._load("funding", coin)
        rows: dict[int, float] = {}
        fetch_from = since_ms
        keep_since = since_ms
        if entry and entry["since_ms"] <= since_ms:
            rows = {int(t): r for t, r in entry["rows"]}
            fetch_from = max(since_ms, entry["fetched_to_ms"] - FUNDING_REREAD_MS)
            keep_since = entry["since_ms"]
        try:
            records = await client.funding_history(coin, fetch_from, end_ms=end_ms,
                                                   page_pause_s=self.funding_page_pause_s)
        except Exception as exc:
            raise RuntimeError(f"{coin}: funding fetch failed ({type(exc).__name__}); backfill deferred") from exc
        self.stats.funding_coins += 1
        for r in records:
            rows[r.ts_ms] = r.rate
        merged = sorted(rows.items())
        self._store("funding", coin, {"coin": coin, "since_ms": keep_since, "fetched_to_ms": end_ms,
                                      "rows": [[t, r] for t, r in merged]})
        if self.funding_page_pause_s:
            await self._sleep(self.funding_page_pause_s)
        return [(t, r) for t, r in merged if t >= since_ms]
