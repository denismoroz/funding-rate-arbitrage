"""EngineLoop v2 — simple async scheduler for the funding-arb engine.

Every minute:
  1. Fetch quotes per coin → save to prices table
  2. Call strategy.on_minute_tick
  3. Refresh wallet snapshots (cash) so it is in sync with live spot_value
  4. Call ledger.compute_and_save

On hour boundary (after minute tasks):
  1. Fetch funding rate per coin → save to funding_rates table
  2. Call strategy.on_hour_tick

No business logic lives here. The loop is pure plumbing.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.coin_registry import CoinRegistry
from frab.db.models import Exchange as ExchangeRow, FundingRate as FundingRateRow, Price as PriceRow, Strategy
from frab.db.session import session_scope
from frab.events.bus import Event, EventBus
from frab.exchanges.protocol import Exchange, WalletKind
from frab.ledger.ledger import Ledger
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase import TwoPhaseParams, TwoPhaseStrategy

logger = logging.getLogger(__name__)

_STOP_TIMEOUT_S = 30.0

# Keep ~14 days of per-minute wallet snapshots; older rows are downsampled to
# one per clock-hour (history is preserved, just thinned). Bounds the table that
# Ledger._compute_cash scans.
_WALLET_SNAPSHOT_RETENTION_MS = 14 * 24 * 60 * 60 * 1000


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
        params_loader: Callable[[dict], object] = TwoPhaseParams.from_dict,
        registry: CoinRegistry | None = None,
        owns_maintenance: bool = True,
    ) -> None:
        self._strategy = strategy
        self._exchange = exchange
        self._ledger = ledger
        self._sf = session_factory
        # When multiple EngineLoops share one SQLite file, table-wide maintenance
        # (wallet_snapshots prune) is global and idempotent — running it on every
        # loop only multiplies the slow DELETE and the write-lock contention it
        # creates at the top of the hour. Exactly one loop should own it.
        self._owns_maintenance = owns_maintenance
        # _coins is the static fallback used when no registry is supplied (and as
        # the initial working set before the first tick resolves open coins).
        self._coins = list(coins)
        self._registry = registry
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

        self._params_loader = params_loader
        self._task: asyncio.Task | None = None
        self._last_hour: int | None = None
        self._exchange_id_cache: int | None = None
        # Last-seen working set: used to detect and log changes (not every tick)
        self._last_working_set: frozenset[str] | None = None

    # ── Working-set resolution ────────────────────────────────────────────────

    async def _resolve_working_coins(self) -> list[str]:
        """Derive the current working coin set for this tick.

        working_coins = registry.universe() ∪ {coins with a non-terminal FarbPosition}

        This ensures that:
        - Newly-activated coins appear immediately (no restart).
        - Deactivated coins with an open position stay quoted/snapshotted so
          their PnL marks and exit state machine keep working.
        - Deactivated coins with no position drop from the set silently.

        Falls back to self._coins when no registry is configured (tests / XSMOM
        engine loop that does not use a CoinRegistry).

        Changes to the working set are logged at INFO level (not every tick).
        """
        if self._registry is None:
            return self._coins

        universe = set(self._registry.universe())

        # Add coins with any open (non-terminal) FarbPosition so exits still work
        open_coins = await self._strategy.farb_repo.distinct_open_coins(
            self._strategy.strategy_id
        )
        working = sorted(universe | open_coins)

        # Log working-set changes (not every tick) to aid ops visibility
        current_frozen = frozenset(working)
        if current_frozen != self._last_working_set:
            if self._last_working_set is None:
                logger.info(
                    "EngineLoop working_set initialized: %s", working
                )
            else:
                added = sorted(current_frozen - self._last_working_set)
                removed = sorted(self._last_working_set - current_frozen)
                logger.info(
                    "EngineLoop working_set changed: added=%s removed=%s current=%s",
                    added, removed, working,
                )
            self._last_working_set = current_frozen

        return working

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

    async def _reload_strategy_params_from_db(self) -> None:
        """Read fresh params_json from DB and rebuild strategy internals if changed."""
        async with session_scope(self._sf) as s:
            row = (await s.execute(
                select(Strategy).where(Strategy.id == self._strategy.strategy_id)
            )).scalar_one()
            new_params = self._params_loader(dict(row.params_json))
        self._strategy.reload_params(new_params)

    async def reload_params_from_db(self) -> None:
        """Public: re-read params_json from DB and rebuild strategy internals now.

        Used by the API (e.g. PATCH /xsmom/params) to apply saved params to the
        running engine immediately, without waiting for the next hourly tick.
        """
        await self._reload_strategy_params_from_db()

    async def force_hour_tick(self, *, strategy_id: int, now_ms: int) -> None:
        """Force an immediate hour tick: reload params from DB, run on_hour_tick.

        Raises StrategyIdMismatch if strategy_id doesn't match the running strategy.
        """
        if self._strategy.strategy_id != strategy_id:
            raise StrategyIdMismatch(self._strategy.strategy_id, strategy_id)

        await self._reload_strategy_params_from_db()

        self._strategy.force_entry_cooldown_bypass = True
        try:
            await self._hour_tick(now_ms)
        finally:
            self._strategy.force_entry_cooldown_bypass = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Main loop body. Sleeps to next minute boundary, then ticks."""
        logger.info("EngineLoop._run: entering loop (static_coins=%s)", self._coins)
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
        """Fetch quotes → persist prices → strategy.on_minute_tick → refresh wallets → ledger.compute_and_save."""
        _t = time.monotonic()
        _stage: dict[str, float] = {}

        def _mark(name: str) -> None:
            nonlocal _t
            now = time.monotonic()
            _stage[name] = now - _t
            _t = now

        # Derive the working coin set each tick (cheap snapshot lookup + one DB query).
        working_coins = await self._resolve_working_coins()
        _mark("working_coins")

        quotes = await self._fetch_quotes(now_ms, working_coins)
        _mark("quotes")
        if quotes:
            await self._save_prices(quotes, now_ms)
        _mark("save_prices")

        await self._strategy.on_minute_tick(now_ms=now_ms)
        _mark("on_minute_tick")

        quote_map = {q.coin: q for q in quotes}
        # Pull authoritative state from the exchange when available
        # (HL: assetPositions[].unrealizedPnl + allMids for spot pairs).
        perp_unrealized_by_coin: dict[str, float] | None = None
        spot_mids_by_coin: dict[str, float] | None = None
        failed_feeds: list[str] = []

        perp_getter = getattr(self._exchange, "get_perp_unrealized_by_coin", None)
        if perp_getter is not None:
            try:
                perp_unrealized_by_coin = await perp_getter()
            except Exception as exc:  # noqa: BLE001
                await self._log_error("perp_unrealized_fetch_failed", exc)
                failed_feeds.append("perp_unrealized")
        _mark("perp_unrealized")
        spot_getter = getattr(self._exchange, "get_spot_mids_by_coin", None)
        if spot_getter is not None:
            try:
                spot_mids_by_coin = await spot_getter()
            except Exception as exc:  # noqa: BLE001
                await self._log_error("spot_mids_fetch_failed", exc)
                failed_feeds.append("spot_mids")
        _mark("spot_mids")

        # Equity snapshot must be all-or-nothing: it is computed from quotes +
        # perp/spot feeds, and a *partial* input set silently prices the missing
        # legs at 0 (spot_value collapses → phantom equity drop persisted to the
        # DB and drawn on the graph). If any input is incomplete, log an event
        # and skip the write entirely — a gap is honest, a phantom drop is not.
        missing_quotes = [c for c in working_coins if c not in quote_map]
        if missing_quotes or failed_feeds:
            await self._log_error(
                "equity_snapshot_skipped",
                RuntimeError(
                    "incomplete equity inputs; snapshot NOT persisted "
                    f"(missing_quotes={missing_quotes}, failed_feeds={failed_feeds})"
                ),
                extra={
                    "missing_quotes": missing_quotes,
                    "failed_feeds": failed_feeds,
                },
            )
            logger.info(
                "minute_tick TIMING sid=%s SKIPPED total=%.2fs stages=%s",
                self._strategy.strategy_id,
                sum(_stage.values()),
                {k: round(v, 2) for k, v in _stage.items()},
            )
            return

        await self._refresh_wallet_snapshots(now_ms)
        _mark("refresh_wallet")
        await self._ledger.compute_and_save(
            self._strategy.strategy_id,
            quote_map,
            perp_unrealized_by_coin=perp_unrealized_by_coin,
            spot_mids_by_coin=spot_mids_by_coin,
        )
        _mark("ledger")
        # TIMING is a stall diagnostic: keep it quiet (DEBUG) on healthy ticks,
        # but escalate to WARNING when a tick runs long so a regression like the
        # old O(N^2) ledger cash scan surfaces in normal logs without spam.
        _total = sum(_stage.values())
        logger.log(
            logging.WARNING if _total >= 30.0 else logging.DEBUG,
            "minute_tick TIMING sid=%s total=%.2fs stages=%s",
            self._strategy.strategy_id,
            _total,
            {k: round(v, 2) for k, v in _stage.items()},
        )

    # ── Hour tick ─────────────────────────────────────────────────────────────

    async def _hour_tick(self, now_ms: int) -> None:
        """Fetch funding → persist → strategy.on_hour_tick."""
        try:
            await self._reload_strategy_params_from_db()
        except Exception:
            logger.exception("reload_params_failed")

        working_coins = await self._resolve_working_coins()
        funding_ticks = await self._fetch_funding(now_ms, working_coins)
        if funding_ticks:
            # funding_rates is a display/signal-history write. A transient SQLite
            # "database is locked" here must NOT skip strategy.on_hour_tick
            # (funding accrual / margin watchdog / rebalance). Isolate it.
            try:
                await self._save_funding(funding_ticks, now_ms)
            except Exception as exc:  # noqa: BLE001
                await self._log_error("save_funding_failed", exc)

        await self._strategy.on_hour_tick(now_ms=now_ms)

        if self._owns_maintenance:
            try:
                await self._prune_wallet_snapshots(now_ms)
            except Exception:
                logger.exception("prune_wallet_snapshots_failed")

    async def _prune_wallet_snapshots(self, now_ms: int) -> None:
        """Downsample old wallet_snapshot rows from per-minute to per-hour.

        wallet_snapshots gains one row per (exchange_id, coin) every minute per
        EngineLoop and is never cleaned, so it grows unbounded — that growth is
        what made Ledger._compute_cash's scan expensive. Rather than dropping
        history (we still want long-term balance statistics), we keep full
        per-minute resolution for the recent window and thin everything older to
        a single row per clock-hour: for each old (exchange_id, coin, hour) we
        keep only the latest row and delete the rest. The freshest row per
        (exchange_id, coin) is always recent (>= cutoff) so it is never touched
        and cash stays computable. Runs hourly; idempotent across the two loops
        that share the table.
        """
        cutoff = now_ms - _WALLET_SNAPSHOT_RETENTION_MS
        async with session_scope(self._sf) as s:
            await s.execute(
                text(
                    "DELETE FROM wallet_snapshots WHERE ts_ms < :cutoff "
                    "AND ts_ms < (SELECT MAX(w2.ts_ms) FROM wallet_snapshots w2 "
                    "WHERE w2.exchange_id = wallet_snapshots.exchange_id "
                    "AND w2.coin = wallet_snapshots.coin "
                    "AND w2.ts_ms / 3600000 = wallet_snapshots.ts_ms / 3600000)"
                ),
                {"cutoff": cutoff},
            )

    # ── Data fetching ─────────────────────────────────────────────────────────

    async def _fetch_quotes(self, now_ms: int, coins: list[str]) -> list:
        """Fetch quotes for the given coins. Per-coin errors are caught; others still run.

        Uses the exchange's batched get_quotes() when available (1 all_mids +
        bounded-parallel l2_book) — far faster than per-coin serial fetch.
        """
        from frab.exchanges.protocol import Quote  # noqa: F401  (kept for type parity)
        results: list = []
        batch = getattr(self._exchange, "get_quotes", None)
        if batch is not None:
            try:
                results = await batch(coins)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._log_error("quotes_batch_failed", exc)
                results = []
        else:
            for coin in coins:
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
        result_map = {r.coin for r in results}
        missed = set(coins) - result_map
        logger.info("_fetch_quotes: coins=%s results_count=%d result_coins=%s missed=%s",
                     coins, len(results), list(result_map), list(missed))
        return results

    async def _fetch_funding(self, now_ms: int, coins: list[str]) -> list:
        """Fetch funding rates for the given coins. Per-coin errors are caught."""
        from frab.exchanges.protocol import FundingTick
        results: list[FundingTick] = []
        for coin in coins:
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
