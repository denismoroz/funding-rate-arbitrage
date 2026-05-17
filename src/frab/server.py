"""Top-level app builder: wires DB + engine + event bus into the FastAPI app."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncIterator

from fastapi import FastAPI
from sqlalchemy import select

from frab.api.app import create_app
from frab.db.models import (
    EquitySnapshot,
    Exchange,
    FundingRate,
    Market,
    Position,
    PositionMode,
    PositionStatus,
    Strategy,
)
from frab.db.recorder import DbRecorder
from frab.db.session import create_engine, make_session_factory, session_scope
from frab.engine.loop import Engine
from frab.events.bus import EventBus, EventDbSink
from frab.exchanges.base import FundingTick
from frab.exchanges.hyperliquid import HLMarketData
from frab.exchanges.paper import PaperExecutor
from frab.settings import get_settings
from frab.strategies.base import Strategy as StrategyBase
from frab.strategies.registry import get_strategy_spec, parse_params_override
from frab.strategies.strategy_a import (
    AccumulatorsSnapshot,
    OpenPositionSnapshot,
)

logger = logging.getLogger(__name__)


DEFAULT_COINS: tuple[str, ...] = ("BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE")
EXCHANGE_NAME = "hyperliquid"


async def _ensure_strategy(session_factory, params_json: dict, *, name: str, version: str, instance_token: str) -> int:
    async with session_scope(session_factory) as s:
        # Defensive cleanup: mark any leftover 'running' strategies as 'stopped'
        # (handles crash recovery when previous process exited uncleanly).
        leftover = await s.execute(
            select(Strategy).where(Strategy.status == "running")
        )
        now_utc = datetime.now(UTC)
        for st in leftover.scalars().all():
            st.status = "stopped"
            st.stopped_at = now_utc
            st.instance_token = None

        # Find or create the strategy row.
        result = await s.execute(
            select(Strategy).where(
                Strategy.name == name,
                Strategy.version == version,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            existing = Strategy(
                name=name,
                version=version,
                params_json=params_json,
                status="idle",
            )
            s.add(existing)
            await s.flush()

        # Mark this strategy as running.
        existing.status = "running"
        existing.started_at = now_utc
        existing.stopped_at = None
        existing.instance_token = instance_token
        return existing.id


async def _mark_stopped_if_owner(session_factory, strategy_id: int, instance_token: str) -> bool:
    """Mark strategy stopped only if its instance_token matches ours.

    Returns True if we updated the row (we were the live owner), False if
    a newer process has already taken ownership (different token).
    """
    async with session_scope(session_factory) as s:
        row = await s.get(Strategy, strategy_id)
        if row is None:
            return False
        if row.instance_token != instance_token:
            return False
        row.status = "stopped"
        row.stopped_at = datetime.now(UTC)
        row.instance_token = None
        return True


async def _load_funding_from_db(
    session_factory,
    strategy: StrategyBase,
    coins: tuple[str, ...],
    window_hours: int,
) -> int:
    """Prime the strategy's MarketState from the latest funding rows in DB.

    Pulls up to `window_hours` rows per coin (most recent first, then reversed
    to ascending order). No network calls — historical data must already have
    been written by `frab backfill` or accumulated by a prior `frab serve` run.
    """
    ticks_by_coin: dict[str, list[FundingTick]] = {}
    async with session_scope(session_factory) as s:
        for coin in coins:
            stmt = (
                select(FundingRate)
                .join(Market, FundingRate.market_id == Market.id)
                .where(Market.coin == coin)
                .order_by(FundingRate.ts.desc())
                .limit(window_hours)
            )
            result = await s.execute(stmt)
            rows = result.scalars().all()
            ticks_by_coin[coin] = [
                FundingTick(
                    coin=coin,
                    ts=fr.ts if fr.ts.tzinfo is not None else fr.ts.replace(tzinfo=UTC),
                    rate=fr.rate,
                    premium=fr.premium,
                    annualized_pct=fr.annualized_pct,
                )
                for fr in reversed(rows)
            ]

    applied = strategy.warmup_from_history(ticks_by_coin)
    per_coin = {c: len(ts) for c, ts in ticks_by_coin.items()}
    logger.info("load_funding_from_db: applied=%d, per_coin=%s", applied, per_coin)
    return applied


async def _rehydrate_strategy_from_db(
    session_factory,
    strategy: StrategyBase,
    strategy_id: int,
) -> None:
    """Restore in-memory positions + cash/accumulators after engine restart.

    Reads OPEN positions and the latest equity snapshot for this strategy.
    Leaves construction defaults in place if nothing persisted (fresh start).
    """
    async with session_scope(session_factory) as s:
        pos_rows = (await s.execute(
            select(Position, Market.coin)
            .join(Market, Position.market_id == Market.id)
            .where(
                Position.strategy_id == strategy_id,
                Position.status == PositionStatus.OPEN,
            )
        )).all()

        last_eq = (await s.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.strategy_id == strategy_id)
            .order_by(EquitySnapshot.ts.desc())
            .limit(1)
        )).scalar_one_or_none()

    snapshots = [
        OpenPositionSnapshot(
            coin=coin,
            opened_at=pos.opened_at if pos.opened_at.tzinfo is not None
                      else pos.opened_at.replace(tzinfo=UTC),
            spot_qty=pos.spot_units,
            perp_qty=abs(pos.perp_units),
            entry_spot_price=pos.entry_spot_price,
            entry_perp_price=pos.entry_perp_price,
            funding_collected=pos.funding_collected,
            fees_paid=pos.fees_paid,
            position_min_hold_hours=pos.position_min_hold_hours,
            consec_negative_hours=pos.consec_negative_hours,
        )
        for pos, coin in pos_rows
    ]

    accumulators = None
    if last_eq is not None:
        accumulators = AccumulatorsSnapshot(
            cash=last_eq.cash,
            realized_pnl_cum=last_eq.perp_realized_cum,
            funding_cum=last_eq.funding_cum,
            fees_cum=last_eq.fees_cum,
        )

    if snapshots or accumulators is not None:
        strategy.rehydrate(positions=snapshots, accumulators=accumulators)
        logger.info(
            "rehydrate_strategy: positions=%d, cash=%s, funding_cum=%s",
            len(snapshots),
            accumulators.cash if accumulators else "default",
            accumulators.funding_cum if accumulators else "default",
        )


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

        spec = get_strategy_spec(settings.strategy_name)
        params_override = parse_params_override(settings.strategy_params_json)
        strategy, params_json = spec.build(
            coins=coins,
            params_override=params_override,
            executor=executor,
        )

        instance_token = uuid.uuid4().hex
        strategy_id = await _ensure_strategy(
            session_factory, params_json, name=spec.name, version=spec.version, instance_token=instance_token
        )
        recorder = DbRecorder(
            session_factory,
            strategy_id=strategy_id,
            exchange_id=exchange_id,
            mode=PositionMode.PAPER,
        )
        await recorder.prime()

        # Derive signal_window_hours from strategy params for DB warmup
        signal_window_hours = params_json.get("signal_window_hours", 12)
        await _load_funding_from_db(
            session_factory, strategy, coins, signal_window_hours
        )
        await _rehydrate_strategy_from_db(session_factory, strategy, strategy_id)
        engine = Engine(
            market_data=market_data,
            strategy=strategy,
            coins=coins,
            recorder=recorder,
            event_bus=bus,
        )
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
        engine_task = asyncio.create_task(engine.run(), name="engine")
        engine_task.add_done_callback(_on_task_done)
        logger.info(
            "frab serve: started (strategy=%s/%s, strategy_id=%d, coins=%s)",
            spec.name, spec.version, strategy_id, coins,
        )

        app.state.strategy = strategy
        app.state.strategy_id = strategy_id
        app.state.engine = engine

        try:
            yield
        finally:
            try:
                updated = await _mark_stopped_if_owner(session_factory, app.state.strategy_id, instance_token)
                if not updated:
                    logger.info(
                        "skipped marking strategy stopped: another process now owns id=%d",
                        app.state.strategy_id,
                    )
            except Exception:
                logger.exception("failed to mark strategy as stopped on shutdown")

            app.state.strategy = None
            app.state.strategy_id = None
            app.state.engine = None
            engine.stop()
            await asyncio.gather(engine_task, return_exceptions=True)
            await sink.stop()
            await asyncio.gather(sink_task, return_exceptions=True)
            await market_data.aclose()
            await db_engine.dispose()
            logger.info("frab serve: shutdown complete")

    app = create_app(session_factory, event_bus=bus)
    app.router.lifespan_context = lifespan
    return app
