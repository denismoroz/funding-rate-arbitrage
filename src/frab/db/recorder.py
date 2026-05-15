"""DB-backed Recorder implementation for Phase 4."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from frab.db.models import (
    EquitySnapshot as EquitySnapshotModel,
    Fill,
    FundingRate,
    Market,
    Position,
    PositionMode,
    PositionStatus,
    Price,
    Signal,
)
from frab.db.session import session_scope
from frab.engine.signals import Decision
from frab.exchanges.base import FundingTick, Leg, Quote, Side
from frab.strategies.base import EquitySnapshot, TickReport

logger = logging.getLogger(__name__)


class DbRecorder:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        strategy_id: int,
        exchange_id: int,
        mode: PositionMode = PositionMode.PAPER,
    ) -> None:
        self._session_factory = session_factory
        self._strategy_id = strategy_id
        self._exchange_id = exchange_id
        self._mode = mode
        self._coin_to_market_id: dict[str, int] = {}
        self._open_positions: dict[str, int] = {}

    async def prime(self) -> None:
        """Resolve coin → market_id map and load currently-open positions."""
        async with session_scope(self._session_factory) as session:
            # Build coin → market_id cache
            result = await session.execute(
                select(Market).where(Market.exchange_id == self._exchange_id)
            )
            markets = result.scalars().all()
            self._coin_to_market_id = {m.coin: m.id for m in markets}

            # Load open positions
            result = await session.execute(
                select(Position, Market.coin)
                .join(Market, Position.market_id == Market.id)
                .where(
                    Position.strategy_id == self._strategy_id,
                    Position.status == PositionStatus.OPEN,
                )
            )
            rows = result.all()
            self._open_positions = {coin: pos.id for pos, coin in rows}

    async def save_quote(self, quote: Quote) -> None:
        market_id = self._coin_to_market_id.get(quote.coin)
        if market_id is None:
            logger.warning("save_quote: unknown coin %r — skipping", quote.coin)
            return

        async with session_scope(self._session_factory) as session:
            existing = await session.scalar(
                select(Price.id).where(
                    Price.market_id == market_id,
                    Price.ts == quote.ts,
                )
            )
            if existing is not None:
                logger.debug(
                    "save_quote: duplicate (market_id=%d, ts=%s) — skipping", market_id, quote.ts
                )
                return
            session.add(
                Price(
                    market_id=market_id,
                    ts=quote.ts,
                    mark=quote.mark,
                    spot=quote.spot,
                    bid=quote.bid,
                    ask=quote.ask,
                )
            )

    async def save_funding(self, tick: FundingTick) -> None:
        market_id = self._coin_to_market_id.get(tick.coin)
        if market_id is None:
            logger.warning("save_funding: unknown coin %r — skipping", tick.coin)
            return

        async with session_scope(self._session_factory) as session:
            existing = await session.scalar(
                select(FundingRate.id).where(
                    FundingRate.market_id == market_id,
                    FundingRate.ts == tick.ts,
                )
            )
            if existing is not None:
                logger.debug(
                    "save_funding: duplicate (market_id=%d, ts=%s) — skipping", market_id, tick.ts
                )
                return
            session.add(
                FundingRate(
                    market_id=market_id,
                    ts=tick.ts,
                    rate=tick.rate,
                    premium=tick.premium,
                    annualized_pct=tick.annualized_pct,
                )
            )

    async def save_tick_report(self, report: TickReport) -> None:
        async with session_scope(self._session_factory) as session:
            # --- Funding accrual on existing open positions ---
            # Applied before opens/closes so that a position closing this tick
            # gets its final funding bump persisted before status flips.
            for coin, delta in report.funding_accrued:
                position_id = self._open_positions.get(coin)
                if position_id is None:
                    logger.warning(
                        "save_tick_report: funding accrued for coin %r but no open position — skipping",
                        coin,
                    )
                    continue
                pos = await session.get(Position, position_id)
                if pos is None:
                    logger.warning(
                        "save_tick_report: position id=%d not found for funding accrual on %r",
                        position_id, coin,
                    )
                    continue
                pos.funding_collected += delta

            # --- Signals ---
            for event in report.signals:
                market_id = self._coin_to_market_id.get(event.coin)
                if market_id is None:
                    logger.warning(
                        "save_tick_report: unknown coin %r in signals — skipping", event.coin
                    )
                    continue
                existing = await session.scalar(
                    select(Signal.id).where(
                        Signal.strategy_id == self._strategy_id,
                        Signal.market_id == market_id,
                        Signal.ts == event.ts,
                    )
                )
                if existing is not None:
                    logger.debug(
                        "save_tick_report: duplicate signal (market_id=%d, ts=%s) — skipping",
                        market_id, event.ts,
                    )
                    continue
                session.add(
                    Signal(
                        strategy_id=self._strategy_id,
                        market_id=market_id,
                        ts=event.ts,
                        signal_value=event.signal_value if event.signal_value is not None else 0.0,
                        regime_pass=event.regime_pass,
                        action=Decision(event.action),
                    )
                )

            # --- Opens ---
            for coin in report.opened:
                market_id = self._coin_to_market_id.get(coin)
                if market_id is None:
                    logger.warning(
                        "save_tick_report: unknown coin %r in opened — skipping", coin
                    )
                    continue

                try:
                    spot_fill = next(
                        f for f in report.fills
                        if f.coin == coin and f.leg == Leg.SPOT and f.side == Side.BUY
                    )
                    perp_fill = next(
                        f for f in report.fills
                        if f.coin == coin and f.leg == Leg.PERP and f.side == Side.SELL
                    )
                except StopIteration:
                    logger.warning(
                        "save_tick_report: missing fills for opened coin %r — skipping", coin
                    )
                    continue

                pos = Position(
                    strategy_id=self._strategy_id,
                    market_id=market_id,
                    mode=self._mode,
                    status=PositionStatus.OPEN,
                    opened_at=report.ts,
                    spot_units=spot_fill.qty,
                    perp_units=-perp_fill.qty,
                    entry_spot_price=spot_fill.price,
                    entry_perp_price=perp_fill.price,
                    fees_paid=spot_fill.fee + perp_fill.fee,
                    realized_pnl=0.0,
                    funding_collected=0.0,
                )
                session.add(pos)
                await session.flush()

                self._open_positions[coin] = pos.id

                session.add(
                    Fill(
                        position_id=pos.id,
                        ts=spot_fill.ts,
                        leg=spot_fill.leg,
                        side=spot_fill.side,
                        qty=spot_fill.qty,
                        price=spot_fill.price,
                        fee=spot_fill.fee,
                        slippage_bps=spot_fill.slippage_bps,
                        is_paper=spot_fill.is_paper,
                    )
                )
                session.add(
                    Fill(
                        position_id=pos.id,
                        ts=perp_fill.ts,
                        leg=perp_fill.leg,
                        side=perp_fill.side,
                        qty=perp_fill.qty,
                        price=perp_fill.price,
                        fee=perp_fill.fee,
                        slippage_bps=perp_fill.slippage_bps,
                        is_paper=perp_fill.is_paper,
                    )
                )

            # --- Closes ---
            for coin in report.closed:
                position_id = self._open_positions.pop(coin, None)
                if position_id is None:
                    logger.warning(
                        "save_tick_report: no open position for closed coin %r — skipping", coin
                    )
                    continue

                pos = await session.get(Position, position_id)
                if pos is None:
                    logger.warning(
                        "save_tick_report: position id=%d not found for coin %r — skipping",
                        position_id,
                        coin,
                    )
                    continue

                try:
                    spot_fill = next(
                        f for f in report.fills
                        if f.coin == coin and f.leg == Leg.SPOT and f.side == Side.SELL
                    )
                    perp_fill = next(
                        f for f in report.fills
                        if f.coin == coin and f.leg == Leg.PERP and f.side == Side.BUY
                    )
                except StopIteration:
                    logger.warning(
                        "save_tick_report: missing close fills for coin %r — skipping", coin
                    )
                    self._open_positions[coin] = position_id  # restore
                    continue

                perp_qty_magnitude = abs(pos.perp_units)
                realized = perp_qty_magnitude * (pos.entry_perp_price - perp_fill.price)
                pos.realized_pnl += realized
                pos.fees_paid += spot_fill.fee + perp_fill.fee
                pos.status = PositionStatus.CLOSED
                pos.closed_at = report.ts
                pos.exit_spot_price = spot_fill.price
                pos.exit_perp_price = perp_fill.price

                session.add(
                    Fill(
                        position_id=pos.id,
                        ts=spot_fill.ts,
                        leg=spot_fill.leg,
                        side=spot_fill.side,
                        qty=spot_fill.qty,
                        price=spot_fill.price,
                        fee=spot_fill.fee,
                        slippage_bps=spot_fill.slippage_bps,
                        is_paper=spot_fill.is_paper,
                    )
                )
                session.add(
                    Fill(
                        position_id=pos.id,
                        ts=perp_fill.ts,
                        leg=perp_fill.leg,
                        side=perp_fill.side,
                        qty=perp_fill.qty,
                        price=perp_fill.price,
                        fee=perp_fill.fee,
                        slippage_bps=perp_fill.slippage_bps,
                        is_paper=perp_fill.is_paper,
                    )
                )

    async def save_equity(self, snapshot: EquitySnapshot) -> None:
        async with session_scope(self._session_factory) as session:
            session.add(
                EquitySnapshotModel(
                    strategy_id=self._strategy_id,
                    ts=snapshot.ts,
                    total_equity=snapshot.total_equity,
                    cash=snapshot.cash,
                    spot_value=snapshot.spot_value,
                    perp_unrealized=snapshot.perp_unrealized,
                    perp_realized_cum=snapshot.perp_realized_cum,
                    funding_cum=snapshot.funding_cum,
                    fees_cum=snapshot.fees_cum,
                )
            )
