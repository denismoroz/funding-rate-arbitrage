"""Top-level app builder: wires DB + event bus + exchange + engine into FastAPI.

Step 8: real HLExchange, TwoPhaseStrategy, EngineLoop wired into lifespan.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncIterator

from fastapi import FastAPI
from sqlalchemy import select

from frab.api.app import create_app
from frab.db.models import Exchange, Strategy as StrategyRow
from frab.db.session import create_engine, make_session_factory, session_scope
from frab.engine.loop import EngineLoop
from frab.events.bus import EventBus, EventDbSink
from frab.exchanges.hyperliquid.exchange import HLExchange
from frab.ledger.ledger import Ledger
from frab.repo.farb_repo import FarbRepo
from frab.settings import Settings, get_settings
from frab.strategy.two_phase import TwoPhaseParams, TwoPhaseStrategy
from frab.exchanges.hyperliquid.tokens import (  # noqa: E402
    MAINNET_SPOT_TOKEN_MAP,
    select_spot_token_map as _select_spot_token_map,
    validate_spot_pairs as _validate_spot_pairs,
)

logger = logging.getLogger(__name__)


DEFAULT_COINS: tuple[str, ...] = ("BTC", "ETH", "SOL")
EXCHANGE_NAME = "hyperliquid"

_STRATEGY_NAME = "two_phase"
_STRATEGY_VERSION = "v2"


def _select_coins(settings: Settings, default: tuple[str, ...]) -> tuple[str, ...]:
    """Universe from settings.hl_universe override, else `default`."""
    override = settings.universe_tuple()
    return override if override else default


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

        # ── Build exchange ────────────────────────────────────────────────
        exchange = HLExchange(
            api_url=_hl_info_url(settings),
            timeout_s=settings.hl_request_timeout_s,
            private_key=(
                settings.hl_private_key.get_secret_value()
                if settings.hl_private_key is not None
                else None
            ),
            account_address=settings.hl_account_address,
            network=settings.hl_network,
            session_factory=session_factory,
            spot_token_map=_select_spot_token_map(settings.hl_network),
            slippage=settings.hl_live_slippage,
        )

        # ── Strategy row lookup/creation ──────────────────────────────────
        strategy_id, params = await _get_or_create_strategy(session_factory)

        # ── Build service layer ───────────────────────────────────────────
        farb_repo = FarbRepo(session_factory)
        ledger = Ledger(session_factory)
        strategy = TwoPhaseStrategy(
            strategy_id=strategy_id,
            exchange=exchange,
            farb_repo=farb_repo,
            session_factory=session_factory,
            params=params,
            event_bus=bus,
        )

        # ── Build and start engine loop ───────────────────────────────────
        engine_loop = EngineLoop(
            strategy=strategy,
            exchange=exchange,
            ledger=ledger,
            session_factory=session_factory,
            coins=list(coins),
            event_bus=bus,
        )
        await engine_loop.start()

        # ── Backfill historical zero-fee fills from HL userFills ──────────
        try:
            n = await exchange.backfill_fill_fees(strategy_id)
            if n:
                logger.info("backfill_fill_fees: updated %d historical fills", n)
        except Exception:
            logger.exception("backfill_fill_fees failed at startup")

        # ── Stash on app.state ────────────────────────────────────────────
        app.state.exchange = exchange
        app.state.farb_repo = farb_repo
        app.state.ledger = ledger
        app.state.strategy = strategy
        app.state.engine_loop = engine_loop

        try:
            yield
        finally:
            await engine_loop.stop()
            await exchange.aclose()
            await sink.stop()
            await asyncio.gather(sink_task, return_exceptions=True)
            await db_engine.dispose()
            logger.info("frab serve: shutdown complete")

    app = create_app(session_factory, event_bus=bus)
    app.router.lifespan_context = lifespan
    return app
