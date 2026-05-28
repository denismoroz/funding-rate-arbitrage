"""Top-level app builder: wires DB + event bus into the FastAPI app.

Strategy/engine/portfolio_service wiring removed in Step 1 (FarbPosition
redesign). Exchange placeholder set to None until Step 2 wires HLExchange.
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
from frab.db.models import (
    Exchange,
    FundingRate,
    Market,
    Strategy,
)
from frab.db.recorder import DbRecorder
from frab.db.session import create_engine, make_session_factory, session_scope
from frab.events.bus import EventBus, EventDbSink
from frab.exchanges.hyperliquid.exchange import HLExchange  # noqa: F401 — available for callers
from frab.settings import Settings, get_settings
from frab.exchanges.hyperliquid.tokens import (  # noqa: E402
    MAINNET_SPOT_TOKEN_MAP,
    select_spot_token_map as _select_spot_token_map,
    validate_spot_pairs as _validate_spot_pairs,
)

logger = logging.getLogger(__name__)


DEFAULT_COINS: tuple[str, ...] = ("BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE")
EXCHANGE_NAME = "hyperliquid"


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


def build_app(coins: tuple[str, ...] = DEFAULT_COINS, *, dry_run: bool = False) -> FastAPI:
    settings = get_settings()
    db_engine = create_engine(settings.db_url)
    session_factory = make_session_factory(db_engine)
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Exchange placeholder — Step 2 will wire the real HLExchange here.
        app.state.exchange = None

        sink = EventDbSink(session_factory, bus)

        def _on_task_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("Background task %s failed", task.get_name(), exc_info=exc)

        sink_task = asyncio.create_task(sink.run(), name="event-sink")
        sink_task.add_done_callback(_on_task_done)
        await sink.wait_until_subscribed()

        try:
            yield
        finally:
            app.state.exchange = None
            await sink.stop()
            await asyncio.gather(sink_task, return_exceptions=True)
            await db_engine.dispose()
            logger.info("frab serve: shutdown complete")

    app = create_app(session_factory, event_bus=bus)
    app.router.lifespan_context = lifespan
    return app
