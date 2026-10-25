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

Backfill (T52b, `retune_backfill`): a book with no state and `params.backfill` first rebuilds the candidates'
past from `backfill_start` with point-in-time inputs (candles and funding from Hyperliquid, market caps from
the q-lab export), so the CURRENT choice is the one the history implies. The history is fetched politely and
cached on disk, so an interrupted backfill resumes; the book itself starts at launch with no fake past trades,
sized to the chosen candidate's current weights. From the first live bar the ranking comes from CoinGecko.
Several `trend_retune*` strategy rows each get their own engine (own capital, state, events) and share the
fetched history.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from frab.db.models import Strategy
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.book import start_book, step
from frab.strategy.trend.cap_history import REPO_ROOT, CapHistory
from frab.strategy.trend.coin_selector import (
    CoinGeckoClient, fetch_cap_snapshot, select_universes, week_key)
from frab.strategy.trend.engine import DAY_MS, HOUR_MS, MAX_CATCHUP_HOURS, _floor_hour
from frab.strategy.trend.retune import BarOutcome, RetuneState, advance_day, build_day
from frab.strategy.trend.retune_backfill import (
    IndexedHistory, ReplayResult, absorb, funding_needs, history_bars, iter_replay, summarize)
from frab.strategy.trend.retune_book import RetuneBook
from frab.strategy.trend.retune_history import HistoryStore
from frab.strategy.trend.retune_params import RetuneParams

logger = logging.getLogger(__name__)


class TrendRetuneEngine:
    def __init__(self, *, session_factory, client, strategy_id: int, repo: TrendRepo | None = None,
                 event_bus=None, clock: Callable[[], int] | None = None, tick_delay_s: float = 255.0,
                 coingecko: CoinGeckoClient | None = None, cap_history: CapHistory | None = None,
                 history_store: HistoryStore | None = None) -> None:
        self._sf = session_factory
        self._client = client
        self._strategy_id = strategy_id
        self._repo = repo or TrendRepo(session_factory, RetuneBook)
        self._bus = event_bus
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._delay = tick_delay_s
        self._coingecko = coingecko
        self._cap_history = cap_history
        self._store = history_store
        self._task: asyncio.Task | None = None
        self._daily_cache: dict[str, tuple[int, int, dict]] = {}
        self.last_tick_ms: int | None = None
        self.last_error: str | None = None
        self.universe: list[str] = []

    # ── lifecycle (as TrendPaperEngine) ──────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"trend-retune-engine-{self._strategy_id}")

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

    async def _perps(self) -> tuple[set[str], set[str]]:
        """(perps Hyperliquid lists now, every perp it still serves a name for, delisted ones included)."""
        meta = await self._client.info_request({"type": "meta"})
        universe = meta.get("universe", [])
        return ({u["name"] for u in universe if not u.get("isDelisted")}, {u["name"] for u in universe})

    async def _listed(self) -> set[str]:
        """Perps Hyperliquid lists now (delisted ones excluded)."""
        return (await self._perps())[0]

    async def _daily(self, names: list[str], start_ms: int, end_ms: int, pause_s: float,
                     complete_through_ms: int | None = None) -> dict:
        """{coin: {open_ms: (close, base volume)}} for every listed coin. A retry within the same hour reuses
        the coins already fetched; so does the first live bar after a backfill (the shared history store)."""
        out: dict[str, dict[int, tuple[float, float | None]]] = {}
        for coin in sorted(names):
            hit = self._daily_cache.get(coin)
            if hit and hit[0] == end_ms and hit[1] <= start_ms:
                out[coin] = hit[2]
                continue
            if self._store is not None and complete_through_ms is not None:
                stored = self._store.peek_daily(coin, start_ms, complete_through_ms)
                if stored is not None:
                    out[coin] = stored
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
        # a ranking rebuilt from q-lab's history is never carried into the live weeks: the first live bar
        # takes CoinGecko's, whatever the week
        stale = snap is None or snap["week"] != week_key(bar_ms) or snap.get("source") != "coingecko"
        if fresh and stale and not tried:
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
        series = await self._daily(sorted(names), start, bars[-1] + HOUR_MS, params.daily_fetch_pause_s,
                                   complete_through_ms=bars[-1])

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

    @staticmethod
    def _mark_first_choice(events: list[dict], state: RetuneState, out: BarOutcome, hour: int, *,
                           launch: bool) -> None:
        """The paper period is counted from the first hour the book follows a chosen candidate. A live
        re-tune that made the choice carries the flag; at launch (the choice is implied by the replayed
        history) a `retune` event of its own records it."""
        for e in out.events:
            if e["kind"] == "retune":
                e["first_choice"] = True
                return
        last = state.last_retune or {}
        events.append(dict(
            kind="retune", coin="-", qty=0.0, price=0.0, notional=0.0, fee=0.0, bar_ms=hour,
            choice=out.chosen, previous_choice=None, score=state.chosen_score, scores=last.get("scores"),
            week=last.get("week"), phase="launch", first_choice=True, implied_by_backfill=launch,
            history_retune_bar_ms=last.get("bar_ms"), cap_source=(state.cap_snapshot or {}).get("source"),
            cap_asof=(state.cap_snapshot or {}).get("snapshot_date")))

    # ── backfill ─────────────────────────────────────────────────────────
    def _backfill_deps(self, params: RetuneParams) -> tuple[HistoryStore, CapHistory]:
        """The shared history store and the market-cap history file; the latter must exist when the backfill
        is on -- starting empty would silently bring back the 2-3 weeks of waiting."""
        if params.trend.rebalance_hour_utc != 0:
            raise RuntimeError("backfill needs rebalance_hour_utc = 0 (daily candles close at 00:00 UTC)")
        if self._cap_history is None:
            try:
                self._cap_history = CapHistory.load(params.cap_history_path)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"backfill is on but the market-cap history {params.cap_history_path!r} "
                                   f"cannot be read ({exc}); deploy the file or set backfill=false") from exc
        if self._store is None:
            cache = Path(params.backfill_cache_dir)
            self._store = HistoryStore(cache if cache.is_absolute() else REPO_ROOT / cache,
                                       candle_pause_s=params.backfill_candle_pause_s,
                                       funding_page_pause_s=params.backfill_funding_page_pause_s)
        return self._store, self._cap_history

    async def _load_history(self, params: RetuneParams, bars: list[int], now_ms: int
                            ) -> IndexedHistory:
        """Candles of every perp and funding of every coin a candidate may hold, from the store (fetching and
        caching what is missing). Resumable: each coin is on disk the moment it is fetched."""
        store, cap = self._backfill_deps(params)
        since = params.backfill_start_ms
        _, names = await self._perps()
        series: dict[str, dict[int, tuple[float, float | None]]] = {}
        coins = sorted(names)
        for i, coin in enumerate(coins, 1):
            series[coin] = await store.daily(self._client, coin, since, now_ms, complete_through_ms=bars[-1])
            if i % 25 == 0:
                logger.info("trend retune backfill: daily candles %d/%d", i, len(coins))
        needs = funding_needs(IndexedHistory(series, {}, since), cap, params, bars)
        funding: dict[str, list[tuple[int, float]]] = {}
        for i, (coin, (first_bar, last_bar)) in enumerate(sorted(needs.items()), 1):
            # the replay's last bar reads funding up to itself; what is earned after it is the live book's
            until = min(last_bar + DAY_MS, bars[-1]) + HOUR_MS
            funding[coin] = await store.funding(self._client, coin, max(since, first_bar - HOUR_MS), until,
                                                complete_through_ms=until)
            if i % 10 == 0:
                logger.info("trend retune backfill: funding %d/%d", i, len(needs))
        return IndexedHistory(series, funding, since)

    async def _ensure_history(self, params: RetuneParams) -> int:
        """Warm the store before the book is created, so the (long) fetch does not stale the launch hour.
        Returns the clock reading the launch is based on."""
        store, _ = self._backfill_deps(params)
        async with store.lock:
            for _ in range(3):
                now = self._clock()
                bars = history_bars(params.backfill_start_ms, _floor_hour(now) - HOUR_MS)
                if not bars:
                    return now
                started = time.monotonic()
                await self._load_history(params, bars, now)
                logger.info("trend retune backfill: history ready in %.0fs (%d candle requests, %d funding "
                            "coins, %d cache hits)", time.monotonic() - started, store.stats.candle_requests,
                            store.stats.funding_coins, store.stats.cache_hits)
                after = self._clock()
                if history_bars(params.backfill_start_ms, _floor_hour(after) - HOUR_MS)[-1:] == bars[-1:]:
                    return after
            return self._clock()

    async def _replay_history(self, params: RetuneParams, first_hour: int, now_ms: int
                              ) -> tuple[ReplayResult, list[int]]:
        """Replay every daily bar before the launch hour through `advance_day` (point-in-time inputs only)."""
        store, cap = self._backfill_deps(params)
        bars = history_bars(params.backfill_start_ms, first_hour)
        if not bars:
            return ReplayResult(state=RetuneState.new(params), last=None), bars
        async with store.lock:
            hist = await self._load_history(params, bars, now_ms)
        state = RetuneState.new(params)
        result = ReplayResult(state=state, last=None)
        started = time.monotonic()
        for _, outcome, universes in iter_replay(hist, cap, params, bars, state):
            absorb(result, outcome, universes)
            await asyncio.sleep(0)                 # ~650 bars of pure CPU: let the event loop breathe
        state.backfill = summarize(result, cap, params, bars) | dict(
            replay_seconds=round(time.monotonic() - started, 1), candle_requests=store.stats.candle_requests,
            funding_coins=store.stats.funding_coins, cache_hits=store.stats.cache_hits)
        if result.universes:
            self.universe = sorted({c for coins in result.universes.values() for c in coins})
        logger.info("trend retune backfill: %d bars replayed, %d re-tunes, chosen=%s", len(bars),
                    state.refits, state.chosen)
        return result, bars

    # ── one tick ─────────────────────────────────────────────────────────
    async def tick(self) -> dict:
        status, params = await self._load()
        now = self._clock()
        self.last_tick_ms = now
        if status != "active":
            return {"status": status, "hours": 0}

        book = await self._repo.get_book(self._strategy_id)
        backfill = book is None and params.backfill
        if backfill:
            now = await self._ensure_history(params)          # long on the first run; resumable
            self.last_tick_ms = now
        last_closed = _floor_hour(now) - HOUR_MS
        first = last_closed if book is None or book.last_bar_ms is None else book.last_bar_ms + HOUR_MS
        if first > last_closed:
            return {"status": status, "hours": 0}
        first = max(first, last_closed - MAX_CATCHUP_HOURS * HOUR_MS)

        if book is None:
            book = RetuneBook.new(params.trend)
        state = RetuneState.from_dict(book.retune, params)
        if book.started and state.chosen and state.first_choice_ms is None:
            # a book that ran before first_choice existed (T52): its first choice is not known any more
            state.first_choice_ms = (book.last_rebalance_ms or book.last_bar_ms or first) + HOUR_MS
        events: list[dict] = []
        launch: BarOutcome | None = None
        if backfill:
            replayed, replay_bars = await self._replay_history(params, first, now)
            state, launch = replayed.state, replayed.last
            events += replayed.events
            if replay_bars:
                events.append(dict(kind="backfill", coin="-", qty=0.0, price=0.0, notional=0.0, fee=0.0,
                                   bar_ms=first - HOUR_MS, **state.backfill))
        bars = [h for h in range(first, last_closed + HOUR_MS, HOUR_MS)
                if (h // HOUR_MS) % 24 == params.trend.rebalance_hour_utc]
        outcomes: dict[int, BarOutcome | None] = {}
        if bars:
            outcomes = await self._simulate(state, params, bars, now)
        live_bars = len(outcomes)
        if launch is not None and outcomes.get(first) is None and launch.chosen:
            # the first hour is not a rebalance bar: size the new book to what the history implies today
            outcomes[first] = BarOutcome(weights=launch.weights, signals=launch.signals,
                                         size_scale=launch.size_scale, chosen=launch.chosen, refit=False,
                                         events=[])
            launch_hour = first
        else:
            launch_hour = None

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
                if out.chosen and state.first_choice_ms is None:
                    state.first_choice_ms = h + HOUR_MS
                    self._mark_first_choice(events, state, out, h, launch=h == launch_hour)
            events += step(book, bar_ms=h, prices=prices, funding=funding, params=params.trend,
                           weights=weights, signals=signals, size_scale=scale)
            eq_rows.append(dict(ts_ms=h + HOUR_MS, equity=book.equity(prices), cash=book.cash,
                                unrealized=book.unrealized(prices), gross_notional=book.gross_notional(prices),
                                net_notional=book.net_notional(prices), legs=book.legs(),
                                funding_total=book.funding_total, fees=book.fees))
        book.retune = state.to_dict()
        await self._repo.save_progress(self._strategy_id, book, events, eq_rows)
        self._daily_cache.clear()
        if backfill and self._store is not None:
            self._store.release()                       # the fetched history stays on disk, not in memory
        if outcomes:
            logger.info("trend retune: %d hour(s), %d bar(s), chosen=%s", len(eq_rows), live_bars, state.chosen)
        return {"status": status, "hours": len(eq_rows), "bars": live_bars,
                "fills": len([e for e in events if e["kind"] not in ("retune", "retune_day", "fund", "backfill")])}
