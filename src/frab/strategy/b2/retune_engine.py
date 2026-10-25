"""Paper engine of the monthly re-tuned Bv2 book (`b2_retune`).

Hourly, shortly after a candle closes: extend the cached history of the book's coins from Hyperliquid's public
info API (`retune_history.HourlyHistoryStore`), advance the simulation (`retune.advance`) through every closed
bar it has not seen, and persist the state, the published per-coin books, fills and equity in ONE transaction.

A new book (no state yet) replays the whole history from the stand's first bar in that same first tick: two
candidates x four coins x ~5500 hours is a fraction of a second of CPU, so this needs no special backfill
machinery. Everything that first tick produces is *backfilled*: `launch_ms` (stored in the state) is the hour it
ran, rows with `ts_ms <= launch_ms` are history and every fill event carries `phase = backfill | live`. The
owner's paper rule counts only the live part.

A restart resumes from the saved state (the next bar after `last_bar_ms`), it never re-replays: replaying would
silently rewrite the published past if the book code ever changed, and the state is small.

PAPER ONLY: the engine is handed a read-only client and never receives a signing key.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from frab.db.models import Strategy
from frab.repo.b2_retune_repo import B2RetuneRepo
from frab.strategy.b2.retune import RetuneState, advance
from frab.strategy.b2.retune_history import HOUR_MS, HourlyHistoryStore, floor_hour
from frab.strategy.b2.retune_params import B2RetuneParams

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]      # <repo>/src/frab/strategy/b2/retune_engine.py -> <repo>
CHUNK_BARS = 250            # the replay yields to the event loop this often


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


class B2RetuneEngine:
    def __init__(self, *, session_factory, client, strategy_id: int, repo: B2RetuneRepo | None = None,
                 event_bus=None, clock: Callable[[], int] | None = None, tick_delay_s: float = 135.0,
                 history_store: HourlyHistoryStore | None = None) -> None:
        self._sf = session_factory
        self._client = client
        self._strategy_id = strategy_id
        self._repo = repo or B2RetuneRepo(session_factory)
        self._bus = event_bus
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._delay = tick_delay_s
        self._store = history_store
        self._task: asyncio.Task | None = None
        self.last_tick_ms: int | None = None
        self.last_error: str | None = None

    # ── lifecycle (as B2PaperEngine) ─────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"b2-retune-engine-{self._strategy_id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await self._safe_tick()
        while True:
            now = self._clock()
            next_ms = floor_hour(now) + HOUR_MS + int(self._delay * 1000)
            await asyncio.sleep(max(1.0, (next_ms - now) / 1000))
            await self._safe_tick()

    async def _safe_tick(self) -> None:
        try:
            await self.tick()
            self.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad hour must not kill the loop
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("b2 retune tick failed")

    # ── one tick ─────────────────────────────────────────────────────────
    async def _load(self) -> tuple[str, B2RetuneParams]:
        async with self._sf() as s:
            row = await s.get(Strategy, self._strategy_id)
        return row.status, B2RetuneParams.from_dict(dict(row.params_json))

    def _history(self, params: B2RetuneParams) -> HourlyHistoryStore:
        if self._store is None:
            self._store = HourlyHistoryStore(_abs(params.history_cache_dir), seed_path=_abs(params.history_seed_path),
                                             candle_pause_s=params.candle_pause_s,
                                             funding_page_pause_s=params.funding_page_pause_s)
        return self._store

    async def tick(self) -> dict:
        status, params = await self._load()
        now = self._clock()
        self.last_tick_ms = now
        if status != "active":
            return {"status": status, "hours": 0}
        last_closed = floor_hour(now) - HOUR_MS             # open time of the newest fully closed candle
        stored = await self._repo.get_state(self._strategy_id)
        state = RetuneState.from_dict(stored, params) if stored else RetuneState.new(params)
        origin = params.history_start_ms
        first = origin if state.last_bar_ms is None else state.last_bar_ms + HOUR_MS
        if first > last_closed:
            return {"status": status, "hours": 0}

        store = self._history(params)
        async with store.lock:
            for coin in params.coins:
                await store.extend(self._client, coin, last_closed, origin)
        series = {coin: store.series(coin, origin, last_closed) for coin in params.coins}
        steppable = [s.ready_through_ms() for s in series.values()]
        if any(r is None for r in steppable):
            raise RuntimeError(f"b2 retune: the history has no steppable bar for "
                               f"{[c for c, r in zip(params.coins, steppable) if r is None]}")
        last = min(last_closed, min(steppable))
        if first > last:
            logger.info("b2 retune: waiting for data (newest closed hour %d, history ready through %d)", last_closed, last)
            return {"status": status, "hours": 0, "waiting": True}

        started = time.monotonic()
        first_run = state.launch_ms is None
        events: list[dict] = []
        equity_rows: list[dict] = []
        views = {}
        n_bars = 0
        for bar in range(first, last + HOUR_MS, HOUR_MS):
            out = advance(state, params, series, bar)
            events += out.events
            equity_rows += out.equity_rows
            views = out.views or views
            n_bars += 1
            if n_bars % CHUNK_BARS == 0:
                await asyncio.sleep(0)                      # ~5500 bars of pure CPU on the first run
        if first_run:
            state.launch_ms = floor_hour(now)
        launch = state.launch_ms
        for e in events:
            e["phase"] = "backfill" if int(e["bar_ms"]) + HOUR_MS <= launch else "live"
        if first_run:
            events.append(dict(kind="backfill", coin="-", qty=0.0, price=0.0, notional=0.0, fee=0.0, bar_ms=last,
                               phase="backfill", from_ms=first, through_ms=last, bars=n_bars, refits=state.refits,
                               chosen=state.chosen, first_choice_ms=state.first_choice_ms, launch_ms=launch,
                               replay_seconds=round(time.monotonic() - started, 2),
                               candle_requests=store.stats.candle_requests,
                               funding_requests=store.stats.funding_coins, history_mismatches=store.mismatches))
            state.backfill = {k: v for k, v in events[-1].items()
                              if k not in ("kind", "coin", "qty", "price", "notional", "fee")}
        await self._repo.save_retune_progress(self._strategy_id, state.to_dict(), views, events, equity_rows)
        logger.info("b2 retune: %d bar(s) through %d, chosen=%s, equity=%s", n_bars, last, state.chosen,
                    None if state.book_equity is None else round(state.book_equity, 2))
        return {"status": status, "hours": n_bars, "chosen": state.chosen, "launch": first_run,
                "fills": len([e for e in events if e["kind"] not in ("retune", "backfill")])}
