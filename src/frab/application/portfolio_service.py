from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from frab.db.models import (
    Exchange as ExchangeRow,
    EquitySnapshot,
    Market,
    Position as DbPosition,
    PositionMode,
    PositionStatus,
)
from frab.db.session import session_scope
from frab.domain.exchange import Exchange
from frab.domain.portfolio import Equity, Portfolio
from frab.domain.position import ClosedPosition, Position
from frab.domain.wallet import WalletInfo


class PortfolioService:
    """Mutable owner of portfolio state. Single source of truth.

    `current()` returns immutable Portfolio snapshot — safe to share
    across tick components.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        strategy_id: int,
        initial_cash_per_exchange: dict[Exchange, float] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._strategy_id = strategy_id
        self._initial_cash_per_exchange: dict[Exchange, float] = (
            initial_cash_per_exchange if initial_cash_per_exchange is not None else {}
        )
        self._positions: dict[tuple[Exchange, str], Position] = {}
        self._cash_per_exchange: dict[Exchange, float] = dict(
            self._initial_cash_per_exchange
        )
        self._fees_cum: float = 0.0
        self._funding_cum: float = 0.0
        self._realized_pnl_cum: float = 0.0
        self._ts: datetime = datetime.now(UTC)

    async def rehydrate_from_db(self) -> None:
        """Load OPEN positions for this strategy_id from DB.

        For each open Position row, build a domain Position using the new
        columns (exchange, state, notional_usd, margin_reserve_usd). Set cash
        per exchange = initial - sum(notional + margin_reserve_usd) for that
        exchange. Read latest EquitySnapshot for strategy_id to seed fees_cum,
        funding_cum, perp_realized_cum.
        """
        async with session_scope(self._session_factory) as session:
            result = await session.execute(
                select(DbPosition, Market.coin, ExchangeRow.name)
                .join(Market, Market.id == DbPosition.market_id)
                .join(ExchangeRow, ExchangeRow.id == Market.exchange_id)
                .where(DbPosition.strategy_id == self._strategy_id)
                .where(DbPosition.status == PositionStatus.OPEN)
            )
            rows = result.all()

        self._positions.clear()
        for db_pos, coin, _exchange_name in rows:
            ex = Exchange(db_pos.exchange)
            pos = Position(
                exchange=ex,
                coin=coin,
                spot_qty=db_pos.spot_units,
                perp_qty=db_pos.perp_units,
                notional_usd=db_pos.notional_usd if db_pos.notional_usd is not None else 0.0,
                margin_reserve_usd=db_pos.margin_reserve_usd if db_pos.margin_reserve_usd is not None else 0.0,
                entry_spot_price=db_pos.entry_spot_price,
                entry_perp_price=db_pos.entry_perp_price,
                opened_at=db_pos.opened_at,
                funding_collected=db_pos.funding_collected,
                fees_paid=db_pos.fees_paid,
                state=db_pos.state if db_pos.state is not None else {},
            )
            self._positions[(ex, coin)] = pos

        async with session_scope(self._session_factory) as session:
            snap_result = await session.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.strategy_id == self._strategy_id)
                .order_by(EquitySnapshot.ts.desc())
                .limit(1)
            )
            snap = snap_result.scalar_one_or_none()
            if snap:
                self._fees_cum = snap.fees_cum
                self._funding_cum = snap.funding_cum
                self._realized_pnl_cum = snap.perp_realized_cum

        for ex, initial in self._initial_cash_per_exchange.items():
            committed = sum(
                p.notional_usd + p.margin_reserve_usd
                for p in self._positions.values()
                if p.exchange == ex
            )
            self._cash_per_exchange[ex] = initial - committed

    async def current(self) -> Portfolio:
        """Return immutable Portfolio snapshot."""
        self._ts = datetime.now(UTC)
        wallet_per_exchange: dict[Exchange, WalletInfo] = {}
        for ex, cash in self._cash_per_exchange.items():
            reserved = sum(
                p.notional_usd + p.margin_reserve_usd
                for p in self._positions.values()
                if p.exchange == ex
            )
            wallet_per_exchange[ex] = WalletInfo(
                exchange=ex,
                available_usdc=cash,
                reserved_usdc=reserved,
                total_value_usd=cash + reserved,
            )
        return Portfolio(
            ts=self._ts,
            positions=tuple(self._positions.values()),
            wallet_per_exchange=wallet_per_exchange,
            fees_cum=self._fees_cum,
            funding_cum=self._funding_cum,
            realized_pnl_cum=self._realized_pnl_cum,
        )

    async def apply_open(self, pos: Position) -> None:
        """Insert new Position row in DB with status=OPEN, mode=LIVE; track in
        _positions; debit pos.notional_usd + pos.margin_reserve_usd from cash.
        """
        async with session_scope(self._session_factory) as session:
            market_result = await session.execute(
                select(Market.id)
                .join(ExchangeRow, ExchangeRow.id == Market.exchange_id)
                .where(ExchangeRow.name == pos.exchange.value)
                .where(Market.coin == pos.coin)
            )
            market_id = market_result.scalar_one_or_none()
            if market_id is None:
                raise ValueError(
                    f"Market not found for exchange={pos.exchange.value!r} coin={pos.coin!r}"
                )
            db_pos = DbPosition(
                strategy_id=self._strategy_id,
                market_id=market_id,
                mode=PositionMode.LIVE,
                status=PositionStatus.OPEN,
                opened_at=pos.opened_at,
                spot_units=pos.spot_qty,
                perp_units=pos.perp_qty,
                entry_spot_price=pos.entry_spot_price,
                entry_perp_price=pos.entry_perp_price,
                realized_pnl=0.0,
                funding_collected=pos.funding_collected,
                fees_paid=pos.fees_paid,
                exchange=pos.exchange.value,
                state=pos.state if pos.state is not None else {},
                notional_usd=pos.notional_usd,
                margin_reserve_usd=pos.margin_reserve_usd,
            )
            session.add(db_pos)

        self._positions[(pos.exchange, pos.coin)] = pos
        if pos.exchange in self._cash_per_exchange:
            self._cash_per_exchange[pos.exchange] -= (
                pos.notional_usd + pos.margin_reserve_usd
            )

    async def apply_close(self, closed: ClosedPosition) -> None:
        """Mark matching DB Position row CLOSED; remove from _positions; credit
        released_notional_usd + released_margin_usd + realized_pnl to cash;
        increment _realized_pnl_cum (reporting counter only).
        """
        async with session_scope(self._session_factory) as session:
            market_result = await session.execute(
                select(Market.id)
                .join(ExchangeRow, ExchangeRow.id == Market.exchange_id)
                .where(ExchangeRow.name == closed.exchange.value)
                .where(Market.coin == closed.coin)
            )
            market_id = market_result.scalar_one_or_none()
            if market_id is None:
                raise ValueError(
                    f"Market not found for exchange={closed.exchange.value!r} coin={closed.coin!r}"
                )
            await session.execute(
                update(DbPosition)
                .where(DbPosition.strategy_id == self._strategy_id)
                .where(DbPosition.market_id == market_id)
                .where(DbPosition.status == PositionStatus.OPEN)
                .values(
                    status=PositionStatus.CLOSED,
                    closed_at=closed.closed_at,
                    realized_pnl=closed.realized_pnl,
                )
            )

        key = (closed.exchange, closed.coin)
        self._positions.pop(key, None)
        if closed.exchange in self._cash_per_exchange:
            self._cash_per_exchange[closed.exchange] += (
                closed.released_notional_usd + closed.released_margin_usd + closed.realized_pnl
            )
        self._realized_pnl_cum += closed.realized_pnl

    async def apply_margin_adjustment(
        self, exchange: Exchange, coin: str, delta_usd: float
    ) -> None:
        """Top-up (+) or release (-) margin. Update Position.margin_reserve_usd in
        DB and in-memory; cash decreases on top-up, increases on release.
        """
        async with session_scope(self._session_factory) as session:
            market_result = await session.execute(
                select(Market.id)
                .join(ExchangeRow, ExchangeRow.id == Market.exchange_id)
                .where(ExchangeRow.name == exchange.value)
                .where(Market.coin == coin)
            )
            market_id = market_result.scalar_one_or_none()
            if market_id is None:
                raise ValueError(
                    f"Market not found for exchange={exchange.value!r} coin={coin!r}"
                )
            db_row = (
                await session.execute(
                    select(DbPosition)
                    .where(DbPosition.strategy_id == self._strategy_id)
                    .where(DbPosition.market_id == market_id)
                    .where(DbPosition.status == PositionStatus.OPEN)
                )
            ).scalar_one()
            db_row.margin_reserve_usd = (
                (db_row.margin_reserve_usd or 0.0) + delta_usd
            )

        key = (exchange, coin)
        if key in self._positions:
            old = self._positions[key]
            self._positions[key] = Position(
                exchange=old.exchange,
                coin=old.coin,
                spot_qty=old.spot_qty,
                perp_qty=old.perp_qty,
                notional_usd=old.notional_usd,
                margin_reserve_usd=old.margin_reserve_usd + delta_usd,
                entry_spot_price=old.entry_spot_price,
                entry_perp_price=old.entry_perp_price,
                opened_at=old.opened_at,
                funding_collected=old.funding_collected,
                fees_paid=old.fees_paid,
                state=old.state,
            )
        if exchange in self._cash_per_exchange:
            self._cash_per_exchange[exchange] -= delta_usd

    async def record_fill_fees(
        self, exchange: Exchange, coin: str, fees: float
    ) -> None:
        """Debit cash (real-wallet semantics); increment _fees_cum (reporting
        counter); increment in-memory and DB Position.fees_paid by `fees`.
        """
        async with session_scope(self._session_factory) as session:
            market_result = await session.execute(
                select(Market.id)
                .join(ExchangeRow, ExchangeRow.id == Market.exchange_id)
                .where(ExchangeRow.name == exchange.value)
                .where(Market.coin == coin)
            )
            market_id = market_result.scalar_one_or_none()
            if market_id is None:
                raise ValueError(
                    f"Market not found for exchange={exchange.value!r} coin={coin!r}"
                )
            db_row = (
                await session.execute(
                    select(DbPosition)
                    .where(DbPosition.strategy_id == self._strategy_id)
                    .where(DbPosition.market_id == market_id)
                    .where(DbPosition.status == PositionStatus.OPEN)
                )
            ).scalar_one()
            db_row.fees_paid = (db_row.fees_paid or 0.0) + fees

        key = (exchange, coin)
        if key in self._positions:
            old = self._positions[key]
            self._positions[key] = Position(
                exchange=old.exchange,
                coin=old.coin,
                spot_qty=old.spot_qty,
                perp_qty=old.perp_qty,
                notional_usd=old.notional_usd,
                margin_reserve_usd=old.margin_reserve_usd,
                entry_spot_price=old.entry_spot_price,
                entry_perp_price=old.entry_perp_price,
                opened_at=old.opened_at,
                funding_collected=old.funding_collected,
                fees_paid=old.fees_paid + fees,
                state=old.state,
            )
        self._fees_cum += fees
        if exchange in self._cash_per_exchange:
            self._cash_per_exchange[exchange] -= fees

    async def accrue_funding(
        self, exchange: Exchange, coin: str, amount: float
    ) -> None:
        """Credit cash (real-wallet semantics); increment _funding_cum (reporting
        counter); increment in-memory and DB Position.funding_collected by `amount`.
        """
        async with session_scope(self._session_factory) as session:
            market_result = await session.execute(
                select(Market.id)
                .join(ExchangeRow, ExchangeRow.id == Market.exchange_id)
                .where(ExchangeRow.name == exchange.value)
                .where(Market.coin == coin)
            )
            market_id = market_result.scalar_one_or_none()
            if market_id is None:
                raise ValueError(
                    f"Market not found for exchange={exchange.value!r} coin={coin!r}"
                )
            db_row = (
                await session.execute(
                    select(DbPosition)
                    .where(DbPosition.strategy_id == self._strategy_id)
                    .where(DbPosition.market_id == market_id)
                    .where(DbPosition.status == PositionStatus.OPEN)
                )
            ).scalar_one()
            db_row.funding_collected = (db_row.funding_collected or 0.0) + amount

        key = (exchange, coin)
        if key in self._positions:
            old = self._positions[key]
            self._positions[key] = Position(
                exchange=old.exchange,
                coin=old.coin,
                spot_qty=old.spot_qty,
                perp_qty=old.perp_qty,
                notional_usd=old.notional_usd,
                margin_reserve_usd=old.margin_reserve_usd,
                entry_spot_price=old.entry_spot_price,
                entry_perp_price=old.entry_perp_price,
                opened_at=old.opened_at,
                funding_collected=old.funding_collected + amount,
                fees_paid=old.fees_paid,
                state=old.state,
            )
        self._funding_cum += amount
        if exchange in self._cash_per_exchange:
            self._cash_per_exchange[exchange] += amount

    async def set_fees_cum(self, value: float) -> None:
        """Authoritative overwrite from reconciler."""
        self._fees_cum = value

    async def set_funding_cum(self, value: float) -> None:
        """Authoritative overwrite from reconciler."""
        self._funding_cum = value

    def equity(self, marks: dict[tuple[Exchange, str], float]) -> Equity:
        """Synchronous; compute equity from real-wallet cash semantics.

        Fees, funding, and realized PnL are already baked into
        _cash_per_exchange (debited/credited on each mutator call). The
        formula is therefore:

            total = cash_total + spot_value + perp_unrealized + margin_reserved

        The _realized_pnl_cum, _funding_cum, _fees_cum fields are kept for
        dashboard/reporting but are NOT double-counted here.
        """
        cash_total = sum(self._cash_per_exchange.values())
        positions = tuple(self._positions.values())
        spot_value = sum(
            p.spot_qty * marks[(p.exchange, p.coin)] for p in positions
        )
        perp_unrealized = sum(
            (p.entry_perp_price - marks[(p.exchange, p.coin)]) * p.perp_qty
            for p in positions
        )
        margin_reserved = sum(p.margin_reserve_usd for p in positions)
        total_equity = cash_total + spot_value + perp_unrealized + margin_reserved
        return Equity(
            ts=datetime.now(UTC),
            total_equity=total_equity,
            cash=cash_total,
            spot_value=spot_value,
            perp_unrealized=perp_unrealized,
            perp_realized_cum=self._realized_pnl_cum,
            funding_cum=self._funding_cum,
            fees_cum=self._fees_cum,
        )
