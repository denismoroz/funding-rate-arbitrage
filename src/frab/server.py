"""Top-level app builder: wires DB + event bus + exchange + engine into FastAPI.

Step 8: real HLExchange, TwoPhaseStrategy, EngineLoop wired into lifespan.
Phase E: second engine (XSMOM) wired in alongside FRAB.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncIterator

from fastapi import FastAPI
from sqlalchemy import select, update

from frab.api.app import create_app
from frab.db.models import Exchange, Strategy as StrategyRow
from frab.db.session import create_engine, make_session_factory, session_scope
from frab.engine.loop import EngineLoop
from frab.events.bus import EventBus, EventDbSink
from frab.exchanges.hyperliquid.exchange import HLExchange
from frab.ledger.ledger import Ledger
from frab.coin_registry import CoinRegistry, RegistryAwareSettings
from frab.repo.coin_registry_repo import CoinRegistryRepo
from frab.repo.farb_repo import FarbRepo
from frab.repo.xsmom_repo import XsmomRepo
from frab.settings import Settings, get_settings
from frab.strategy.two_phase import TwoPhaseParams, TwoPhaseStrategy
from frab.strategy.xsmom.params import XsmomParams
from frab.strategy.xsmom.strategy import XsmomStrategy
from frab.strategy.xsmom.protection.margin_watchdog import XsmomMarginWatchdog
from frab.exchanges.hyperliquid.tokens import (  # noqa: E402
    MAINNET_SPOT_TOKEN_MAP,
    select_spot_token_map as _select_spot_token_map,
    validate_spot_pairs as _validate_spot_pairs,
)

logger = logging.getLogger(__name__)


DEFAULT_COINS: tuple[str, ...] = ("BTC", "ETH", "SOL", "HYPE", "PURR")
EXCHANGE_NAME = "hyperliquid"

_STRATEGY_NAME = "two_phase"
_STRATEGY_VERSION = "v2"

_XSMOM_STRATEGY_NAME = "xsmom"
_XSMOM_STRATEGY_VERSION = "v1"


def _select_coins(registry: CoinRegistry) -> tuple[str, ...]:
    """Universe from CoinRegistry (active + validated coins).

    Falls back to DEFAULT_COINS when the registry is empty (e.g. test with
    no seeded DB), preserving backward compatibility during Phase B.
    """
    coins = registry.universe()
    return coins if coins else DEFAULT_COINS


def _hl_info_url(settings: Settings) -> str:
    """Return the /info endpoint URL for HLExchangeReader based on network."""
    from hyperliquid.utils import constants
    if settings.hl_network == "testnet":
        return f"{constants.TESTNET_API_URL}/info"
    return f"{constants.MAINNET_API_URL}/info"


async def _resolve_exchange(session_factory) -> int:
    async with session_scope(session_factory) as s:
        result = await s.execute(select(Exchange).where(Exchange.name == EXCHANGE_NAME))
        exc = result.scalar_one_or_none()
        if exc is None:
            raise RuntimeError(
                f"Exchange {EXCHANGE_NAME!r} not seeded; run `frab seed` first."
            )
        return exc.id


async def _get_or_create_strategy(session_factory) -> tuple[int, TwoPhaseParams]:
    """Return (strategy_id, params). Creates row with defaults if absent."""
    async with session_scope(session_factory) as s:
        result = await s.execute(
            select(StrategyRow).where(
                StrategyRow.name == _STRATEGY_NAME,
                StrategyRow.version == _STRATEGY_VERSION,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            default_params = TwoPhaseParams()
            import dataclasses
            params_dict = dataclasses.asdict(default_params)
            row = StrategyRow(
                name=_STRATEGY_NAME,
                version=_STRATEGY_VERSION,
                params_json=params_dict,
                status="active",
                started_at_ms=int(datetime.now(UTC).timestamp() * 1000),
            )
            s.add(row)
            await s.flush()
            strategy_id = row.id
            params = default_params
            logger.info(
                "Created strategy row id=%s name=%s version=%s",
                strategy_id, _STRATEGY_NAME, _STRATEGY_VERSION,
            )
        else:
            strategy_id = row.id
            params = TwoPhaseParams.from_dict(dict(row.params_json))
            logger.info(
                "Loaded existing strategy row id=%s name=%s version=%s status=%s",
                strategy_id, row.name, row.version, row.status,
            )
    return strategy_id, params


async def _pause_all_strategies(session_factory) -> int:
    """Set status='paused' on ALL strategy rows. Returns the count updated.

    Used in local_mode to ensure no strategy starts trading on boot.
    """
    async with session_scope(session_factory) as s:
        result = await s.execute(
            update(StrategyRow).values(status="paused")
        )
        return result.rowcount


async def _get_or_create_xsmom_strategy(
    session_factory,
) -> tuple[int, XsmomParams]:
    """Return (strategy_id, params) for the xsmom strategy row.

    Creates the row with status='paused' and default params if absent.
    """
    async with session_scope(session_factory) as s:
        result = await s.execute(
            select(StrategyRow).where(
                StrategyRow.name == _XSMOM_STRATEGY_NAME,
                StrategyRow.version == _XSMOM_STRATEGY_VERSION,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            # Seed from XsmomParams() defaults (like FRAB seeds from TwoPhaseParams()).
            # budget_cap / universe / leverage are strategy settings — the operator
            # configures them via the UI (PATCH /api/xsmom/params) before enabling.
            params = XsmomParams()
            row = StrategyRow(
                name=_XSMOM_STRATEGY_NAME,
                version=_XSMOM_STRATEGY_VERSION,
                params_json=params.to_dict(),
                status="paused",
                started_at_ms=int(datetime.now(UTC).timestamp() * 1000),
            )
            s.add(row)
            await s.flush()
            strategy_id = row.id
            logger.info(
                "Created xsmom strategy row id=%s name=%s version=%s status=paused",
                strategy_id, _XSMOM_STRATEGY_NAME, _XSMOM_STRATEGY_VERSION,
            )
        else:
            strategy_id = row.id
            params = XsmomParams.from_dict(dict(row.params_json))
            logger.info(
                "Loaded existing xsmom strategy row id=%s name=%s version=%s status=%s",
                strategy_id, row.name, row.version, row.status,
            )
    return strategy_id, params


def build_app(coins: tuple[str, ...] = DEFAULT_COINS, *, dry_run: bool = False) -> FastAPI:
    settings = get_settings()
    db_engine = create_engine(settings.db_url)
    session_factory = make_session_factory(db_engine)
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        def _on_task_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("Background task %s failed", task.get_name(), exc_info=exc)

        sink = EventDbSink(session_factory, bus)
        sink_task = asyncio.create_task(sink.run(), name="event-sink")
        sink_task.add_done_callback(_on_task_done)
        await sink.wait_until_subscribed()

        # ── Load CoinRegistry BEFORE building the exchange/engine ─────────
        # Registry supplies universe, spot-maps, and coin specs at runtime.
        # Derived snapshot dicts are passed into HLExchange / EngineLoop so
        # every deep call-site gets registry values without going async.
        registry = CoinRegistry(session_factory)
        await registry.load()
        app.state.coin_registry = registry

        # ── Phase C: startup validation gate ─────────────────────────────
        # Defensive: universe() already filters active AND validated_at IS NOT NULL
        # (Phase B invariant).  Log a hard error if somehow a coin slips through.
        # This should never happen in a well-formed DB, but fail loud if it does.
        _universe_coins = registry.universe()
        _all_rows = await CoinRegistryRepo(session_factory).list()
        _invalid_active = [
            r.coin for r in _all_rows
            if r.active and r.validated_at is None
        ]
        if _invalid_active:
            # Log at ERROR level and abort startup — a coin is active but not validated.
            raise RuntimeError(
                f"Startup validation gate: the following coin(s) are active=True but "
                f"validated_at IS NULL in coin_registry — they must be validated before "
                f"activation (Phase C gate): {_invalid_active}.  "
                f"Deactivate or validate these coins before starting the engine."
            )
        logger.info(
            "Startup validation gate: OK — active universe=%s, all active coins have validated_at set.",
            _universe_coins,
        )

        # RegistryAwareSettings wraps the plain Settings and overrides
        # get_coin_spec() to read from the registry instead of RESEARCH_LEVERAGE.
        # All other settings attributes pass through transparently.
        registry_settings = RegistryAwareSettings(settings, registry)

        # Registry-derived coin universe (active + validated rows).
        # Falls back to the `coins` arg (CLI / test default) when DB is empty.
        active_coins = _select_coins(registry) if registry.universe() else coins

        # Spot-token map: from registry on mainnet, empty on testnet.
        if settings.hl_network == "mainnet":
            spot_map = registry.spot_token_map()
        else:
            spot_map = {}
        spot_inverse = registry.spot_token_inverse()

        # ── Build exchange ────────────────────────────────────────────────
        exchange = HLExchange(
            api_url=_hl_info_url(settings),
            timeout_s=settings.hl_request_timeout_s,
            sdk_timeout_s=settings.hl_sdk_timeout_s,
            private_key=(
                settings.hl_private_key.get_secret_value()
                if settings.hl_private_key is not None
                else None
            ),
            account_address=settings.hl_account_address,
            network=settings.hl_network,
            session_factory=session_factory,
            spot_token_map=spot_map,
            spot_token_inverse=spot_inverse,
            slippage=settings.hl_live_slippage,
        )

        # ── Strategy row lookup/creation ──────────────────────────────────
        strategy_id, params = await _get_or_create_strategy(session_factory)

        # ── Build service layer ───────────────────────────────────────────
        farb_repo = FarbRepo(session_factory)
        ledger = Ledger(session_factory, account=settings.hl_account_address)

        from frab.engine.margin_manager import MarginManager
        from frab.engine.margin_watchdog import MarginWatchdog

        margin_mgr = MarginManager(
            top_up_trigger=settings.top_up_trigger,
            forced_close_trigger=settings.forced_close_trigger,
            healthy_ratio=settings.healthy_ratio,
        )
        margin_watchdog = MarginWatchdog(
            strategy_id=strategy_id,
            exchange=exchange,
            farb_repo=farb_repo,
            margin_manager=margin_mgr,
            settings=registry_settings,
            event_bus=bus,
        )

        strategy = TwoPhaseStrategy(
            strategy_id=strategy_id,
            exchange=exchange,
            farb_repo=farb_repo,
            session_factory=session_factory,
            params=params,
            settings=registry_settings,
            event_bus=bus,
            margin_watchdog=margin_watchdog,
            registry=registry,
        )

        # ── local_mode: pause ALL strategies before any loop starts ─────────
        # Loops are still started (background schedulers run), but on_hour_tick
        # reads status from DB and skips trading when paused — so "started but
        # paused" satisfies the requirement: loops run, no entries/rebalance
        # until UI toggle enables a strategy.
        if settings.local_mode:
            n_paused = await _pause_all_strategies(session_factory)
            logger.info(
                "local_mode=true: paused %d strategy row(s); loops will start but no trading until UI toggle",
                n_paused,
            )

        # ── Build and start engine loop ───────────────────────────────────
        engine_loop = EngineLoop(
            strategy=strategy,
            exchange=exchange,
            ledger=ledger,
            session_factory=session_factory,
            coins=list(active_coins),
            event_bus=bus,
            registry=registry,
        )
        await engine_loop.start()

        # ── Backfill historical zero-fee fills from HL userFills ──────────
        try:
            n = await exchange.backfill_fill_fees(strategy_id)
            if n:
                logger.info("backfill_fill_fees: updated %d historical fills", n)
        except Exception:
            logger.exception("backfill_fill_fees failed at startup")

        # ── XSMOM engine (only if xsmom credentials are configured) ──────
        if settings.has_xsmom_credentials():
            xsmom_exchange = HLExchange(
                api_url=_hl_info_url(settings),
                timeout_s=settings.hl_request_timeout_s,
                sdk_timeout_s=settings.hl_sdk_timeout_s,
                private_key=settings.xsmom_hl_private_key.get_secret_value(),
                account_address=settings.xsmom_hl_account_address,
                network=settings.hl_network,
                session_factory=session_factory,
                spot_token_map=spot_map,
                spot_token_inverse=spot_inverse,
                slippage=settings.hl_live_slippage,
            )
            xsmom_strategy_id, xsmom_params = await _get_or_create_xsmom_strategy(
                session_factory
            )
            xsmom_repo = XsmomRepo(session_factory)
            xsmom_ledger = Ledger(
                session_factory, account=settings.xsmom_hl_account_address
            )

            xsmom_margin_mgr = MarginManager(
                top_up_trigger=settings.top_up_trigger,
                forced_close_trigger=settings.forced_close_trigger,
                healthy_ratio=settings.healthy_ratio,
            )
            xsmom_watchdog = XsmomMarginWatchdog(
                strategy_id=xsmom_strategy_id,
                exchange=xsmom_exchange,
                xsmom_repo=xsmom_repo,
                margin_manager=xsmom_margin_mgr,
                registry=registry,
                event_bus=bus,
            )
            xsmom_strategy = XsmomStrategy(
                strategy_id=xsmom_strategy_id,
                exchange=xsmom_exchange,
                xsmom_repo=xsmom_repo,
                session_factory=session_factory,
                params=xsmom_params,
                settings=settings,
                event_bus=bus,
                margin_watchdog=xsmom_watchdog,
            )
            xsmom_loop = EngineLoop(
                strategy=xsmom_strategy,
                exchange=xsmom_exchange,
                ledger=xsmom_ledger,
                session_factory=session_factory,
                coins=list(xsmom_params.universe),
                event_bus=bus,
                params_loader=XsmomParams.from_dict,
            )
            await xsmom_loop.start()

            app.state.xsmom_exchange = xsmom_exchange
            app.state.xsmom_repo = xsmom_repo
            app.state.xsmom_strategy = xsmom_strategy
            app.state.xsmom_loop = xsmom_loop
            app.state.xsmom_strategy_id = xsmom_strategy_id
            app.state.xsmom_ledger = xsmom_ledger
            app.state.xsmom_margin_watchdog = xsmom_watchdog
            logger.info("xsmom engine started (strategy_id=%s)", xsmom_strategy_id)
        else:
            logger.info("xsmom credentials not set; skipping xsmom engine")

        # ── Stash on app.state ────────────────────────────────────────────
        app.state.exchange = exchange
        app.state.farb_repo = farb_repo
        app.state.ledger = ledger
        app.state.strategy = strategy
        app.state.engine_loop = engine_loop
        app.state.margin_watchdog = margin_watchdog

        try:
            yield
        finally:
            # Stop xsmom engine first (if it was built)
            xsmom_loop_state = getattr(app.state, "xsmom_loop", None)
            if xsmom_loop_state is not None:
                await xsmom_loop_state.stop()
            xsmom_exchange_state = getattr(app.state, "xsmom_exchange", None)
            if xsmom_exchange_state is not None:
                await xsmom_exchange_state.aclose()

            await engine_loop.stop()
            await exchange.aclose()
            await sink.stop()
            await asyncio.gather(sink_task, return_exceptions=True)
            await db_engine.dispose()
            logger.info("frab serve: shutdown complete")

    app = create_app(session_factory, event_bus=bus)
    app.router.lifespan_context = lifespan
    return app
