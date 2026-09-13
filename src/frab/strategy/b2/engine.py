"""Strategy B v2 paper engine.

Once per hour, shortly after the candle closes: pull closed hourly candles and funding from
Hyperliquid's public info API, advance every coin book through each closed bar it has not
seen yet (so a restart or a missed hour is caught up, not skipped), persist state + fills +
equity atomically per coin.

PAPER ONLY: the engine is handed a read-only client and never receives a signing key, so it
cannot place an order even by mistake.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from sqlalchemy import select

from frab.db.models import Strategy
from frab.repo.b2_repo import B2Repo
from frab.strategy.b2.book import CoinBook, advance_signals, start_book, step
from frab.strategy.b2.params import B2Params

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
HISTORY_BARS = 740          # > 30 days of hourly closes for the 30d momentum
WARMUP_BARS = 48            # enough to rebuild the sticky-exit state exactly
MAX_CATCHUP_BARS = 24 * 30  # one tick never replays more than 30 days


def _floor_hour(ms: int) -> int:
    return ms - ms % HOUR_MS


class B2PaperEngine:
    def __init__(self, *, session_factory, client, strategy_id: int, repo: B2Repo | None = None,
                 event_bus=None, clock: Callable[[], int] | None = None, tick_delay_s: float = 75.0) -> None:
        self._sf = session_factory
        self._client = client
        self._strategy_id = strategy_id
        self._repo = repo or B2Repo(session_factory)
        self._bus = event_bus
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._delay = tick_delay_s
        self._task: asyncio.Task | None = None
        self.last_tick_ms: int | None = None
        self.last_error: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="b2-paper-engine")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await self._safe_tick()                     # catch up immediately on start
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
            logger.exception("b2 paper tick failed")

    # ── one tick ─────────────────────────────────────────────────────────
    async def _load(self) -> tuple[str, B2Params]:
        async with self._sf() as s:
            row = await s.get(Strategy, self._strategy_id)
        return row.status, B2Params.from_dict(dict(row.params_json))

    async def tick(self) -> dict:
        status, params = await self._load()
        now = self._clock()
        self.last_tick_ms = now
        if status != "active":
            return {"status": status, "processed": {}}
        last_closed = _floor_hour(now) - HOUR_MS          # open time of the newest fully closed candle
        processed = {}
        for coin in params.coins:
            processed[coin] = await self._advance_coin(coin, params, last_closed)
        return {"status": status, "processed": processed}

    async def _advance_coin(self, coin: str, params: B2Params, last_closed: int) -> int:
        book = await self._repo.get_book(self._strategy_id, coin)
        first = last_closed if book is None or book.last_bar_ms is None else book.last_bar_ms + HOUR_MS
        if first > last_closed:
            return 0
        first = max(first, last_closed - MAX_CATCHUP_BARS * HOUR_MS)
        hist_start = first - (HISTORY_BARS + WARMUP_BARS) * HOUR_MS

        candles = await self._client.candle_snapshot(coin, "1h", hist_start, last_closed + HOUR_MS)
        closes = {c.open_ms: c.close for c in candles}
        funding_recs = await self._client.funding_history(coin, hist_start)
        funding = {_floor_hour(r.ts_ms): r.rate for r in funding_recs}

        hours = list(range(hist_start, last_closed + HOUR_MS, HOUR_MS))
        # History may start after hist_start for young coins; the bars we trade on may not have gaps.
        missing = [h for h in hours if h >= first and h not in closes]
        if missing:
            logger.warning("b2 %s: %d closed candle(s) missing from %s; waiting for data", coin, len(missing), missing[0])
            return 0
        avail = [h for h in hours if h in closes]
        px = [closes[h] for h in avail]
        fr = [funding.get(h, 0.0) for h in avail]
        pos = {h: i for i, h in enumerate(avail)}

        if book is None:
            book = CoinBook.new(coin, params)
        events: list[dict] = []
        if not book.started:
            self._warm_signals(book, px, fr, pos[first], params)
            events += start_book(book, bar_ms=first, price=px[pos[first]], params=params)

        eq_rows = []
        for h in range(first, last_closed + HOUR_MS, HOUR_MS):
            i = pos[h]
            events += step(book, bar_ms=h, price=px[i], funding_rate=fr[i],
                           closes=px[max(0, i - HISTORY_BARS + 9):i + 1],
                           funding_hist=fr[max(0, i - 8):i + 1], params=params)
            p = px[i]
            eq_rows.append(dict(ts_ms=h + HOUR_MS, price=p, equity=book.equity(p), book_equity=book.book_equity(p),
                                cash=book.cash, spot_value=book.units_spot * p, short_pnl=book.short_pnl(p),
                                carry_cash=book.carry_cash, hedge_on=book.in_pos, carry_on=book.carry_on))
        await self._repo.save_progress(self._strategy_id, book, events, eq_rows)
        if events:
            logger.info("b2 %s: %d bar(s), fills: %s", coin, len(eq_rows), [e["kind"] for e in events])
        return len(eq_rows)

    @staticmethod
    def _warm_signals(book: CoinBook, px: list[float], fr: list[float], i_first: int, params: B2Params) -> None:
        """Rebuild the previous-bar signal state a book would carry had it been running."""
        for i in range(max(0, i_first - WARMUP_BARS), i_first):
            advance_signals(book, px[max(0, i - HISTORY_BARS + 9):i + 1], fr[max(0, i - 8):i + 1], params)
