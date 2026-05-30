"""EngineLoop v2 — simple async scheduler for the funding-arb engine.

Every minute:
  1. Fetch quotes per coin → save to prices table
  2. Call strategy.on_minute_tick
  3. Call ledger.compute_and_save

On hour boundary (after minute tasks):
  1. Fetch funding rate per coin → save to funding_rates table
  2. Refresh wallet snapshots
  3. Call strategy.on_hour_tick

No business logic lives here. The loop is pure plumbing.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Exchange as ExchangeRow, FundingRate as FundingRateRow, Price as PriceRow, Strategy
from frab.db.session import session_scope
from frab.events.bus import Event, EventBus
from frab.exchanges.protocol import Exchange, WalletKind
from frab.ledger.ledger import Ledger
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase import TwoPhaseParams, TwoPhaseStrategy

logger = logging.getLogger(__name__)

_STOP_TIMEOUT_S = 30.0


class StrategyIdMismatch(Exception):
    def __init__(self, expected: int, got: int) -> None:
        super().__init__(f"strategy_id mismatch: engine is running id={expected}, got id={got}")
        self.expected = expected
        self.got = got


def utcnow_ms() -> int:
    """Return current UTC timestamp in milliseconds. Patchable in tests."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class EngineLoop:
    """Background asyncio scheduler: minute tick + hour tick.

    Usage::

        loop = EngineLoop(strategy=..., exchange=..., ledger=..., ...)
        await loop.start()   # spawns background task, returns immediately
        ...
        await loop.stop()    # graceful shutdown (waits for current tick)
    """

    def __init__(
        self,
        *,
        strategy: TwoPhaseStrategy,
        exchange: Exchange,
        ledger: Ledger,
        session_factory: async_sessionmaker[AsyncSession],
        coins: list[str],
        event_bus: EventBus | None = None,
        minute_interval_s: float = 60.0,
        wallet_coins: list[tuple[str, WalletKind]] | None = None,
    ) -> None:
        self._strategy = strategy
        self._exchange = exchange
        self._ledger = ledger
        self._sf = session_factory
        self._coins = list(coins)
        self._bus = event_bus
        self._minute_interval_s = minute_interval_s
        # Default wallet targets for snapshot refresh
        if wallet_coins is None:
            self._wallet_coins: list[tuple[str, WalletKind]] = [
                ("USDC", WalletKind.SPOT),
                ("USDC", WalletKind.PERP),
            ]
        else:
            self._wallet_coins = list(wallet_coins)

        self._task: asyncio.Task | None = None
        self._last_hour: int | None = None
        self._exchange_id_cache: int | None = None

    # ── Public lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the background loop task. Idempotent: second call is a no-op."""
        if self._task is not None and not self._task.done():
            logger.debug("EngineLoop.start() called but loop is already running")
            return
        self._task = asyncio.create_task(self._run(), name="engine-loop")

        def _on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("EngineLoop background task died", exc_info=exc)

        self._task.add_done_callback(_on_done)
        logger.info("EngineLoop started")

    async def stop(self) -> None:
        """Request graceful shutdown. Waits up to 30 s, then cancels. No-op if not running."""
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=_STOP_TIMEOUT_S)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        logger.info("EngineLoop stopped")

    async def force_hour_tick(self, *, strategy_id: int, now_ms: int) -> None:
        """Force an immediate hour tick: reload params from DB, run on_hour_tick.

        Raises StrategyIdMismatch if strategy_id doesn't match the running strategy.
        """
        if self._strategy.strategy_id != strategy_id:
            raise StrategyIdMismatch(self._strategy.strategy_id, strategy_id)

        async with session_scope(self._sf) as s:
            row = (await s.execute(
                select(Strategy).where(Strategy.id == strategy_id)
            )).scalar_one()
            self._strategy.params = TwoPhaseParams.from_dict(dict(row.params_json))

        self._strategy.force_entry_cooldown_bypass = True
        try:
            await self._hour_tick(now_ms)
        finally:
            self._strategy.force_entry_cooldown_bypass = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Main loop body. Sleeps to next minute boundary, then ticks."""
        logger.info("EngineLoop._run: entering loop (coins=%s)", self._coins)
        while True:
            now_ms = utcnow_ms()
            # Sleep to the next minute boundary (or next interval for tests)
            secs_into_interval = (now_ms / 1000) % self._minute_interval_s
            sleep_s = self._minute_interval_s - secs_into_interval
            await asyncio.sleep(sleep_s)

            now_ms = utcnow_ms()
            current_hour = int(now_ms / 3_600_000)

            try:
                await self._minute_tick(now_ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._log_error("minute_tick_failed", exc)

            if self._last_hour is None or current_hour != self._last_hour:
                try:
                    await self._hour_tick(now_ms)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    await self._log_error("hour_tick_failed", exc)
                self._last_hour = current_hour

    # ── Minute tick ───────────────────────────────────────────────────────────

    async def _minute_tick(self, now_ms: int) -> None:
        """Fetch quotes → persist prices → strategy.on_minute_tick → ledger.compute_and_save."""
        quotes = await self._fetch_quotes(now_ms)
        if quotes:
            await self._save_prices(quotes, now_ms)

        await self._strategy.on_minute_tick(now_ms=now_ms)

        quote_map = {q.coin: q for q in quotes}
        # Pull authoritative state from the exchange when available
        # (HL: assetPositions[].unrealizedPnl + allMids for spot pairs).
        # Ledger falls back to local computation if either is missing.
        perp_unrealized_by_coin: dict[str, float] | None = None
        spot_mids_by_coin: dict[str, float] | None = None
        perp_getter = getattr(self._exchange, "get_perp_unrealized_by_coin", None)
        if perp_getter is not None:
            try:
                perp_unrealized_by_coin = await perp_getter()
            except Exception as exc:  # noqa: BLE001
                await self._log_error("perp_unrealized_fetch_failed", exc)
        spot_getter = getattr(self._exchange, "get_spot_mids_by_coin", None)
        if spot_getter is not None:
            try:
                spot_mids_by_coin = await spot_getter()
            except Exception as exc:  # noqa: BLE001
                await self._log_error("spot_mids_fetch_failed", exc)
        await self._ledger.compute_and_save(
            self._strategy.strategy_id,
            quote_map,
            perp_unrealized_by_coin=perp_unrealized_by_coin,
            spot_mids_by_coin=spot_mids_by_coin,
        )

    # ── Hour tick ─────────────────────────────────────────────────────────────

    async def _hour_tick(self, now_ms: int) -> None:
        """Fetch funding → persist → refresh wallet snapshots → strategy.on_hour_tick."""
        funding_ticks = await self._fetch_funding(now_ms)
        if funding_ticks:
            await self._save_funding(funding_ticks, now_ms)

        await self._refresh_wallet_snapshots(now_ms)
        await self._strategy.on_hour_tick(now_ms=now_ms)

    # ── Data fetching ─────────────────────────────────────────────────────────

    async def _fetch_quotes(self, now_ms: int) -> list:
        """Fetch quotes for all coins. Per-coin errors are caught; others still run."""
        from frab.exchanges.protocol import Quote
        results: list[Quote] = []
        for coin in self._coins:
            try:
                quote = await self._exchange.get_quote(coin)
                results.append(quote)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._log_error(
                    "quote_fetch_failed",
                    exc,
                    extra={"coin": coin},
                )
        return results

    async def _fetch_funding(self, now_ms: int) -> list:
        """Fetch funding rates for all coins. Per-coin errors are caught."""
        from frab.exchanges.protocol import FundingTick
        results: list[FundingTick] = []
        for coin in self._coins:
            try:
                tick = await self._exchange.get_funding_rate(coin)
                results.append(tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._log_error(
                    "funding_fetch_failed",
                    exc,
                    extra={"coin": coin},
                )
        return results

    # ── DB persistence ────────────────────────────────────────────────────────

    async def _resolve_exchange_id(self) -> int:
        """Look up exchange row by name. Cached after first successful lookup."""
        if self._exchange_id_cache is None:
            async with session_scope(self._sf) as s:
                result = await s.execute(
                    select(ExchangeRow).where(ExchangeRow.name == self._exchange.name)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    raise RuntimeError(
                        f"Exchange {self._exchange.name!r} not in DB; run `frab seed` first."
                    )
                self._exchange_id_cache = row.id
        return self._exchange_id_cache

    async def _save_prices(self, quotes, now_ms: int) -> None:
        exchange_id = await self._resolve_exchange_id()
        async with session_scope(self._sf) as s:
            for quote in quotes:
                stmt = (
                    sqlite_insert(PriceRow)
                    .values(
                        exchange_id=exchange_id,
                        coin=quote.coin,
                        ts_ms=now_ms,
                        mark=quote.mark,
                        spot=quote.spot,
                        bid=quote.bid,
                        ask=quote.ask,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["exchange_id", "coin", "ts_ms"]
                    )
                )
                await s.execute(stmt)

    async def _save_funding(self, ticks, now_ms: int) -> None:
        exchange_id = await self._resolve_exchange_id()
        async with session_scope(self._sf) as s:
            for tick in ticks:
                stmt = (
                    sqlite_insert(FundingRateRow)
                    .values(
                        exchange_id=exchange_id,
                        coin=tick.coin,
                        ts_ms=tick.ts_ms if tick.ts_ms else now_ms,
                        rate=tick.rate,
                        premium=tick.premium,
                        annualized_pct=tick.annualized_pct,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["exchange_id", "coin", "ts_ms"]
                    )
                )
                await s.execute(stmt)

    async def _refresh_wallet_snapshots(self, now_ms: int) -> None:
        """Refresh wallet balance snapshots. Exchange.get_wallet writes to DB internally."""
        for coin, kind in self._wallet_coins:
            try:
                await self._exchange.get_wallet(coin, kind)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._log_error(
                    "wallet_snapshot_failed",
                    exc,
                    extra={"coin": coin, "kind": kind.value},
                )

    # ── Error logging ─────────────────────────────────────────────────────────

    async def _log_error(
        self, kind: str, exc: Exception, extra: dict | None = None
    ) -> None:
        tb = traceback.format_exc()
        payload: dict = {"error": str(exc), "traceback": tb}
        if extra:
            payload.update(extra)
        logger.error("engine_loop %s: %s", kind, exc, exc_info=True)
        if self._bus is not None:
            from datetime import timezone
            event = Event(
                ts=datetime.now(timezone.utc),
                level="ERROR",
                source="engine_loop",
                kind=kind,
                message=str(exc),
                payload_json=payload,
            )
            await self._bus.publish(event)
