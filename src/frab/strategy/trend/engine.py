"""Trend paper engine.

Once per hour, shortly after the candle closes: pull closed hourly candles and funding from Hyperliquid's
public info API for every coin of the book, advance the book through each closed hour it has not seen yet
(so a restart or a missed hour is caught up, not skipped), and once a day — at the rebalance hour, on the
daily candles closed by then — resize the whole book to the day's target weights. State, fills and equity
are persisted atomically per tick.

PAPER ONLY: the engine is handed a read-only client and never receives a signing key, so it cannot place
an order even by mistake.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from frab.db.models import Strategy
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.book import TrendBook, start_book, step
from frab.strategy.trend.params import TrendParams
from frab.strategy.trend.signals import ensemble_signal, target_weights

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MAX_CATCHUP_HOURS = 24 * 7          # one tick never replays more than a week


def _floor_hour(ms: int) -> int:
    return ms - ms % HOUR_MS


class TrendPaperEngine:
    def __init__(self, *, session_factory, client, strategy_id: int, repo: TrendRepo | None = None,
                 event_bus=None, clock: Callable[[], int] | None = None, tick_delay_s: float = 195.0) -> None:
        self._sf = session_factory
        self._client = client
        self._strategy_id = strategy_id
        self._repo = repo or TrendRepo(session_factory)
        self._bus = event_bus
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._delay = tick_delay_s
        self._task: asyncio.Task | None = None
        self.last_tick_ms: int | None = None
        self.last_error: str | None = None
        self.universe: list[str] = []

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="trend-paper-engine")

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
            logger.exception("trend paper tick failed")

    # ── one tick ─────────────────────────────────────────────────────────
    async def _load(self) -> tuple[str, TrendParams]:
        async with self._sf() as s:
            row = await s.get(Strategy, self._strategy_id)
        return row.status, TrendParams.from_dict(dict(row.params_json))

    async def _fetch(self, coin: str, params: TrendParams, hour_from: int, hour_to: int) -> dict:
        """Hourly closes, hourly funding and daily closes for one coin."""
        candles = await self._client.candle_snapshot(coin, "1h", hour_from, hour_to + HOUR_MS)
        daily = await self._client.candle_snapshot(coin, "1d", hour_to - params.history_days * DAY_MS,
                                                   hour_to + HOUR_MS)
        funding = await self._client.funding_history(coin, hour_from)
        return dict(hourly={c.open_ms: c.close for c in candles},
                    daily=sorted(((c.close_ms, c.close) for c in daily)),
                    funding={_floor_hour(r.ts_ms): r.rate for r in funding})

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

        data = {}
        for coin in params.coins:
            try:
                data[coin] = await self._fetch(coin, params, first - 2 * HOUR_MS, last_closed)
            except Exception as exc:  # noqa: BLE001 — one bad coin must not stop the book
                logger.warning("trend %s: fetch failed (%s); coin skipped this tick", coin, exc)
        self.universe = sorted(c for c, d in data.items() if len(d["daily"]) >= params.min_history_days)

        if book is None:
            book = TrendBook.new(params)
        events: list[dict] = []
        fresh_start = not book.started
        if fresh_start:
            events += start_book(book, bar_ms=first - HOUR_MS, params=params)

        eq_rows = []
        for h in range(first, last_closed + HOUR_MS, HOUR_MS):
            prices = {c: d["hourly"].get(h, book.prices.get(c)) for c, d in data.items()}
            prices = {c: p for c, p in prices.items() if p}
            funding = {c: d["funding"].get(h, 0.0) for c, d in data.items()}
            weights = signals = None
            # daily at the rebalance hour; a brand-new book also sizes itself on its very first bar
            if (h // HOUR_MS) % 24 == params.rebalance_hour_utc or (fresh_start and h == first):
                closes = {c: [px for ts, px in d["daily"] if ts <= h] for c, d in data.items()}
                weights = target_weights(closes, params)
                signals = {c: ensemble_signal(v, params) for c, v in closes.items()
                           if len(v) >= params.min_history_days}
                weights = {c: w for c, w in weights.items() if c in prices}
            events += step(book, bar_ms=h, prices=prices, funding=funding, params=params,
                           weights=weights, signals=signals)
            eq_rows.append(dict(ts_ms=h + HOUR_MS, equity=book.equity(prices), cash=book.cash,
                                unrealized=book.unrealized(prices), gross_notional=book.gross_notional(prices),
                                net_notional=book.net_notional(prices), legs=book.legs(),
                                funding_total=book.funding_total, fees=book.fees))
        await self._repo.save_progress(self._strategy_id, book, events, eq_rows)
        if events:
            logger.info("trend: %d hour(s), fills: %s", len(eq_rows),
                        [(e["kind"], e["coin"]) for e in events])
        return {"status": status, "hours": len(eq_rows), "fills": len(events)}
