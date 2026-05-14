"""Async tick loop that drives a Strategy on minute and hour cadences."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable, Protocol, runtime_checkable

from frab.exchanges.base import FundingTick, MarketDataSource, Quote
from frab.strategies.base import EquitySnapshot, Strategy, TickReport


@runtime_checkable
class Recorder(Protocol):
    async def save_quote(self, quote: Quote) -> None: ...
    async def save_funding(self, tick: FundingTick) -> None: ...
    async def save_tick_report(self, report: TickReport) -> None: ...
    async def save_equity(self, snapshot: EquitySnapshot) -> None: ...


class NullRecorder:
    async def save_quote(self, quote: Quote) -> None:
        return None

    async def save_funding(self, tick: FundingTick) -> None:
        return None

    async def save_tick_report(self, report: TickReport) -> None:
        return None

    async def save_equity(self, snapshot: EquitySnapshot) -> None:
        return None


@dataclass(frozen=True, slots=True)
class TickOutcome:
    ts: datetime
    quotes: dict[str, Quote]
    funding: dict[str, FundingTick] | None  # None if not crossing an hour boundary
    tick_report: TickReport | None          # None if not crossing an hour boundary
    equity: EquitySnapshot


class Engine:
    def __init__(
        self,
        *,
        market_data: MarketDataSource,
        strategy: Strategy,
        coins: tuple[str, ...],
        recorder: Recorder | None = None,
        clock_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if len(coins) == 0:
            raise ValueError("coins must be non-empty")
        self._market_data = market_data
        self._strategy = strategy
        self._coins = tuple(coins)
        self._recorder = recorder if recorder is not None else NullRecorder()
        self._clock_fn = clock_fn if clock_fn is not None else (lambda: datetime.now(UTC))
        self._sleep = sleep_fn if sleep_fn is not None else asyncio.sleep
        self._stop = False
        self._last_hour: datetime | None = None

    def stop(self) -> None:
        self._stop = True

    async def tick_once(self, now: datetime) -> TickOutcome:
        # 1. Fetch quotes concurrently
        quote_tasks = [self._market_data.fetch_quote(coin) for coin in self._coins]
        quote_list = await asyncio.gather(*quote_tasks)
        quotes = {coin: q for coin, q in zip(self._coins, quote_list)}

        # 2. Save quotes
        for q in quote_list:
            await self._recorder.save_quote(q)

        # 3. Call minute tick
        await self._strategy.on_minute_tick(now, quotes)

        # 4. Hour boundary check
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        crossed_hour = self._last_hour is None or current_hour != self._last_hour

        # 5. Hour-tick branch
        if crossed_hour:
            funding_tasks = [self._market_data.fetch_funding(coin) for coin in self._coins]
            funding_list = await asyncio.gather(*funding_tasks)
            funding = {coin: f for coin, f in zip(self._coins, funding_list)}
            for f in funding_list:
                await self._recorder.save_funding(f)
            tick_report = await self._strategy.on_hour_tick(now, funding)
            await self._recorder.save_tick_report(tick_report)
            self._last_hour = current_hour
        else:
            funding = None
            tick_report = None

        # 6. Equity snapshot (every tick)
        equity = self._strategy.compute_equity(now)
        await self._recorder.save_equity(equity)

        # 7. Return
        return TickOutcome(
            ts=now,
            quotes=quotes,
            funding=funding,
            tick_report=tick_report,
            equity=equity,
        )

    async def run(self) -> None:
        while not self._stop:
            now = self._clock_fn()
            next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
            delay_s = max(0.0, (next_minute - now).total_seconds())
            await self._sleep(delay_s)
            if self._stop:
                break
            await self.tick_once(next_minute)
