"""Top-level app builder: wires DB + engine + event bus into the FastAPI app."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from sqlalchemy import select

from frab.api.app import create_app
from frab.db.models import Exchange, PositionMode, Strategy
from frab.db.recorder import DbRecorder
from frab.db.session import create_engine, make_session_factory, session_scope
from frab.engine.loop import Engine
from frab.events.bus import EventBus, EventDbSink
from frab.exchanges.hyperliquid import HLMarketData
from frab.exchanges.paper import PaperExecutor
from frab.settings import get_settings
from frab.strategies.strategy_a import StrategyA, StrategyAParams

logger = logging.getLogger(__name__)


DEFAULT_COINS: tuple[str, ...] = ("BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE")
STRATEGY_NAME = "strategy_a"
STRATEGY_VERSION = "v1"
EXCHANGE_NAME = "hyperliquid"


async def _ensure_strategy(session_factory, params_json: dict) -> int:
    async with session_scope(session_factory) as s:
        result = await s.execute(
            select(Strategy).where(
                Strategy.name == STRATEGY_NAME,
                Strategy.version == STRATEGY_VERSION,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing.id
        row = Strategy(
            name=STRATEGY_NAME,
            version=STRATEGY_VERSION,
            params_json=params_json,
            status="idle",
        )
        s.add(row)
        await s.flush()
        return row.id


async def _resolve_exchange(session_factory) -> tuple[int, float, float]:
    async with session_scope(session_factory) as s:
        result = await s.execute(select(Exchange).where(Exchange.name == EXCHANGE_NAME))
        exc = result.scalar_one_or_none()
        if exc is None:
            raise RuntimeError(
                f"Exchange {EXCHANGE_NAME!r} not seeded; run `frab seed` first."
            )
        return exc.id, exc.spot_taker_bps, exc.perp_taker_bps


def build_app(coins: tuple[str, ...] = DEFAULT_COINS) -> FastAPI:
    settings = get_settings()
    db_engine = create_engine(settings.db_url)
    session_factory = make_session_factory(db_engine)
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        params = StrategyAParams(coins=coins)
        params_json = {
            "coins": list(coins),
            "entry_threshold": params.entry_threshold,
            "exit_threshold": params.exit_threshold,
            "min_hold_hours": params.min_hold_hours,
            "signal_window_hours": params.signal_window_hours,
            "concurrency_cap": params.concurrency_cap,
            "position_size_usdc": params.position_size_usdc,
        }
        strategy_id = await _ensure_strategy(session_factory, params_json)
        exchange_id, spot_bps, perp_bps = await _resolve_exchange(session_factory)

        market_data = HLMarketData(
            api_url=settings.hl_api_url,
            timeout_s=settings.hl_request_timeout_s,
        )
        executor = PaperExecutor(
            market_data=market_data,
            spot_taker_bps=spot_bps,
            perp_taker_bps=perp_bps,
            extra_slip_bps=settings.paper_extra_slip_bps,
        )
        strategy = StrategyA(params=params, executor=executor)
        recorder = DbRecorder(
            session_factory,
            strategy_id=strategy_id,
            exchange_id=exchange_id,
            mode=PositionMode.PAPER,
        )
        await recorder.prime()
        engine = Engine(
            market_data=market_data,
            strategy=strategy,
            coins=coins,
            recorder=recorder,
        )
        sink = EventDbSink(session_factory, bus)

        engine_task = asyncio.create_task(engine.run(), name="engine")
        sink_task = asyncio.create_task(sink.run(), name="event-sink")
        logger.info("frab serve: engine + sink started (strategy_id=%d, coins=%s)", strategy_id, coins)

        try:
            yield
        finally:
            engine.stop()
            await sink.stop()
            await asyncio.gather(engine_task, sink_task, return_exceptions=True)
            await market_data.aclose()
            await db_engine.dispose()
            logger.info("frab serve: shutdown complete")

    app = create_app(session_factory, event_bus=bus)
    app.router.lifespan_context = lifespan
    return app
