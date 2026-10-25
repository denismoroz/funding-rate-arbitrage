"""Paper engine of the weekly re-tuned trend variant (`trend_retune`).

Same hourly loop and the same paper book as `engine.TrendPaperEngine` (funding every hour, the book resized
at the rebalance hour), but WHAT the book is resized to comes from `retune.advance_day`: ten candidates
(coin-selection rule x lookback set) are simulated every day on Hyperliquid's own daily candles, and once a
week the one with the best past Sharpe after costs is the one traded.

Per tick:
 1. If the hours not yet seen contain a daily rebalance bar: list the HL perps, pull daily candles of ALL
    of them (the leverage cap normalises over the whole universe, see retune.py), refresh the market-cap
    ranking when the ISO week changed, pick the universes (selector), pull funding for the coins the
    candidates hold, and run the simulation bar by bar.
 2. Pull hourly candles and funding only for the coins the book holds or is about to hold.
 3. Advance the book hour by hour and save book + selector state + events + equity in one transaction.

All or nothing, like the plain trend engine: a failed fetch defers the tick and the next one replays the hours.
PAPER ONLY: the client is read-only, no signing key exists here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from frab.db.models import Strategy
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.book import start_book, step
from frab.strategy.trend.coin_selector import (
    CoinGeckoClient, fetch_cap_snapshot, select_universes, week_key)
from frab.strategy.trend.engine import DAY_MS, HOUR_MS, MAX_CATCHUP_HOURS, _floor_hour
from frab.strategy.trend.retune import BarOutcome, RetuneState, advance_day, build_day
from frab.strategy.trend.retune_book import RetuneBook
from frab.strategy.trend.retune_params import RetuneParams

logger = logging.getLogger(__name__)


class TrendRetuneEngine:
    def __init__(self, *, session_factory, client, strategy_id: int, repo: TrendRepo | None = None,
                 event_bus=None, clock: Callable[[], int] | None = None, tick_delay_s: float = 255.0,
                 coingecko: CoinGeckoClient | None = None) -> None:
        self._sf = session_factory
        self._client = client
        self._strategy_id = strategy_id
        self._repo = repo or TrendRepo(session_factory, RetuneBook)
        self._bus = event_bus
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._delay = tick_delay_s
        self._coingecko = coingecko
        self._task: asyncio.Task | None = None
        self._daily_cache: dict[str, tuple[int, int, dict]] = {}
        self.last_tick_ms: int | None = None
        self.last_error: str | None = None
        self.universe: list[str] = []

    # ── lifecycle (as TrendPaperEngine) ──────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="trend-retune-engine")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._coingecko is not None:
            await self._coingecko.aclose()

    async def _run(self) -> None:
        await self._safe_tick()
        while True:
            now = self._clock()
            next_ms = _floor_hour(now) + HOUR_MS + int(self._delay * 1000)
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
            logger.exception("trend retune tick failed")

    # ── data ─────────────────────────────────────────────────────────────
    async def _load(self) -> tuple[str, RetuneParams]:
        async with self._sf() as s:
            row = await s.get(Strategy, self._strategy_id)
        return row.status, RetuneParams.from_dict(dict(row.params_json))

    async def _listed(self) -> set[str]:
        """Perps Hyperliquid lists now (delisted ones excluded)."""
        meta = await self._client.info_request({"type": "meta"})
        return {u["name"] for u in meta.get("universe", []) if not u.get("isDelisted")}

    async def _daily(self, names: list[str], start_ms: int, end_ms: int, pause_s: float) -> dict:
        """{coin: {open_ms: (close, base volume)}} for every listed coin. A retry within the same hour reuses
        the coins already fetched."""
        out: dict[str, dict[int, tuple[float, float | None]]] = {}
        for coin in sorted(names):
            hit = self._daily_cache.get(coin)
            if hit and hit[0] == end_ms and hit[1] <= start_ms:
                out[coin] = hit[2]
                continue
            try:
                candles = await self._client.candle_snapshot(coin, "1d", start_ms, end_ms)
            except Exception as exc:
                raise RuntimeError(f"{coin}: daily fetch failed ({type(exc).__name__}); tick deferred") from exc
            data = {c.open_ms: (c.close, getattr(c, "volume", None)) for c in candles}
            self._daily_cache[coin] = (end_ms, start_ms, data)
            out[coin] = data
            if pause_s:
                await asyncio.sleep(pause_s)
        return out

    async def _funding(self, coins: set[str], since_ms: int) -> dict[str, list[tuple[int, float]]]:
        out = {}
        for coin in sorted(coins):
            try:
                recs = await self._client.funding_history(coin, since_ms)
            except Exception as exc:
                raise RuntimeError(f"{coin}: funding fetch failed ({type(exc).__name__}); tick deferred") from exc
            out[coin] = [(r.ts_ms, r.rate) for r in recs]
        return out

    async def _hourly(self, coin: str, hour_from: int, hour_to: int) -> dict:
        candles = await self._client.candle_snapshot(coin, "1h", hour_from, hour_to + HOUR_MS)
        funding = await self._client.funding_history(coin, hour_from)
        return dict(hourly={c.open_ms: c.close for c in candles},
                    funding={_floor_hour(r.ts_ms): r.rate for r in funding})

    # ── the daily simulation ─────────────────────────────────────────────
    async def _cap_snapshot(self, bar_ms: int, now_ms: int, params: RetuneParams, state: RetuneState,
                            hl_names: set[str], tried: list) -> dict | None:
        """The market-cap ranking a bar may use. A bar no older than `selection_max_age_hours` takes a fresh
        snapshot when the ISO week changed; an older one (catch-up replay) only reuses what was known."""
        snap = state.cap_snapshot
        fresh = bar_ms >= now_ms - int(params.selection_max_age_hours * HOUR_MS)
        if fresh and (snap is None or snap["week"] != week_key(bar_ms)) and not tried:
            tried.append(True)
            try:
                if self._coingecko is None:
                    self._coingecko = CoinGeckoClient(params.coingecko_base_url)
                snap = await fetch_cap_snapshot(self._coingecko, hl_names, taken_ms=now_ms,
                                                week=week_key(bar_ms), rank_limit=params.market_cap_rank_limit)
            except Exception as exc:  # noqa: BLE001
                if snap is None:
                    raise RuntimeError(f"market caps unavailable ({type(exc).__name__}); tick deferred") from exc
                logger.warning("market-cap refresh failed (%s); keeping the ranking of week %s", exc, snap["week"])
        return snap

    async def _simulate(self, state: RetuneState, params: RetuneParams, bars: list[int], now_ms: int
                        ) -> dict[int, BarOutcome | None]:
        names = await self._listed()
        trend = params.trend
        start = min(bars[0], state.last_bar_ms or bars[0]) - (trend.history_days + 3) * DAY_MS
        series = await self._daily(sorted(names), start, bars[-1] + HOUR_MS, params.daily_fetch_pause_s)

        # pass 1: universes per bar (no simulation state involved except the market-cap snapshot)
        plan: list[tuple[int, dict | None, dict | None]] = []
        tried: list = []
        snap_state = RetuneState(cap_snapshot=state.cap_snapshot)
        for bar in bars:
            snap = await self._cap_snapshot(bar, now_ms, params, snap_state, names, tried)
            snap_state.cap_snapshot = snap
            if snap is None:
                plan.append((bar, None, None))          # nothing point-in-time to rank by: skip the bar
                continue
            day0 = build_day(series, {}, bar, None)
            universes = select_universes(
                cap_ranked=[r[0] for r in snap["ranked"]],
                closes_count={c: len(v) for c, v in day0.closes.items()}, tradeable=set(day0.price_now),
                volume_usd=day0.volume_usd, k_cap=params.top_k_by_market_cap, k_volume=params.top_k_by_volume,
                min_history=trend.min_history_days if params.top_k_requires_history else None)
            plan.append((bar, snap, universes))

        held = {c for cand in state.candidates.values() for c in cand.prev_weights}
        needed = set(held)
        for _, _, u in plan:
            for coins in (u or {}).values():
                needed.update(coins)
        since = (state.last_bar_ms or bars[0] - DAY_MS) - 2 * HOUR_MS
        funding = await self._funding(needed, since)

        # pass 2: the simulation, bar by bar
        outcomes: dict[int, BarOutcome | None] = {}
        for bar, snap, universes in plan:
            if snap is None or universes is None:
                outcomes[bar] = None
                logger.warning("trend retune: bar %s skipped, no market-cap ranking yet", bar)
                continue
            state.cap_snapshot = snap
            day = build_day(series, funding, bar, state.last_bar_ms)
            outcomes[bar] = advance_day(state, day, params, universes)
        done = [u for _, _, u in plan if u]
        if done:
            self.universe = sorted({c for coins in done[-1].values() for c in coins})
        return outcomes

    # ── one tick ─────────────────────────────────────────────────────────
    async def tick(self) -> dict:
        status, params = await self._load()
        now = self._clock()
        self.last_tick_ms = now
        if status != "active":
            return {"status": status, "hours": 0}

        last_closed = _floor_hour(now) - HOUR_MS
        book = await self._repo.get_book(self._strategy_id)
        first = last_closed if book is None or book.last_bar_ms is None else book.last_bar_ms + HOUR_MS
        if first > last_closed:
            return {"status": status, "hours": 0}
        first = max(first, last_closed - MAX_CATCHUP_HOURS * HOUR_MS)

        if book is None:
            book = RetuneBook.new(params.trend)
        state = RetuneState.from_dict(book.retune, params)
        bars = [h for h in range(first, last_closed + HOUR_MS, HOUR_MS)
                if (h // HOUR_MS) % 24 == params.trend.rebalance_hour_utc]
        outcomes: dict[int, BarOutcome | None] = {}
        if bars:
            outcomes = await self._simulate(state, params, bars, now)

        held = set(book.units)
        for o in outcomes.values():
            if o:
                held.update(o.weights)
        hourly = {}
        for coin in sorted(held):
            try:
                hourly[coin] = await self._hourly(coin, first - 2 * HOUR_MS, last_closed)
            except Exception as exc:
                raise RuntimeError(f"{coin}: fetch failed ({type(exc).__name__}); tick deferred") from exc

        events: list[dict] = []
        if not book.started:
            events += start_book(book, bar_ms=first - HOUR_MS, params=params.trend)
        eq_rows = []
        for h in range(first, last_closed + HOUR_MS, HOUR_MS):
            prices = {c: d["hourly"].get(h, book.prices.get(c)) for c, d in hourly.items()}
            prices = {c: p for c, p in prices.items() if p}
            funding = {c: d["funding"].get(h, 0.0) for c, d in hourly.items()}
            weights = signals = None
            scale = None
            out = outcomes.get(h)
            if out is not None:
                events += out.events
                weights = {c: w for c, w in out.weights.items() if c in prices}
                signals, scale = out.signals, out.size_scale
            events += step(book, bar_ms=h, prices=prices, funding=funding, params=params.trend,
                           weights=weights, signals=signals, size_scale=scale)
            eq_rows.append(dict(ts_ms=h + HOUR_MS, equity=book.equity(prices), cash=book.cash,
                                unrealized=book.unrealized(prices), gross_notional=book.gross_notional(prices),
                                net_notional=book.net_notional(prices), legs=book.legs(),
                                funding_total=book.funding_total, fees=book.fees))
        book.retune = state.to_dict()
        await self._repo.save_progress(self._strategy_id, book, events, eq_rows)
        self._daily_cache.clear()
        if outcomes:
            logger.info("trend retune: %d hour(s), %d bar(s), chosen=%s", len(eq_rows), len(outcomes), state.chosen)
        return {"status": status, "hours": len(eq_rows), "bars": len(outcomes),
                "fills": len([e for e in events if e["kind"] not in ("retune", "retune_day", "fund")])}
