"""PaperExchange: simulated exchange implementation.

Delegates quote/funding/meta to an upstream Exchange (live HL) for fresh data.
Simulates fills with configurable slippage and fees. Writes to DB like the live
exchange. No instance-level state caches — every read goes to DB.

PaperExchange exchange_id strategy: lazy create-or-fetch on first DB write.
The `exchanges` table row is created with name="paper" if it doesn't exist.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from frab.db.models import (
    Exchange as DBExchange,
    Fill as DBFill,
    FundingAccrual as DBFundingAccrual,
    FundingRate as DBFundingRate,
    Position as DBPosition,
    WalletSnapshot as DBWalletSnapshot,
)
from frab.db.session import session_scope
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.protocol import (
    Exchange,
    FundingTick,
    MarketSpec,
    OpenRequest,
    Quote,
    WalletKind,
)

logger = logging.getLogger(__name__)

_PAPER_EXCHANGE_SPEC = {
    "name": "paper",
    "funding_interval_h": 1,
    "spot_taker_bps": 7.0,
    "perp_taker_bps": 3.5,
}


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class PaperExchange:
    """Paper (simulated) exchange. Uses upstream for market data, simulates fills."""

    name = "paper"

    def __init__(
        self,
        *,
        upstream: Exchange,
        session_factory: async_sessionmaker[AsyncSession],
        fee_bps_spot: float,
        fee_bps_perp: float,
        extra_slip_bps: float = 2.0,
    ) -> None:
        self._upstream = upstream
        self._session_factory = session_factory
        self._fee_bps_spot = fee_bps_spot
        self._fee_bps_perp = fee_bps_perp
        self._extra_slip_bps = extra_slip_bps
        self._exchange_id: int | None = None

    # ------------------------------------------------------------------
    # Internal: DB helpers
    # ------------------------------------------------------------------

    async def _get_or_create_exchange_id(self, session: AsyncSession) -> int:
        result = await session.execute(
            select(DBExchange).where(DBExchange.name == self.name)
        )
        exc = result.scalar_one_or_none()
        if exc is None:
            exc = DBExchange(
                name=_PAPER_EXCHANGE_SPEC["name"],
                funding_interval_h=_PAPER_EXCHANGE_SPEC["funding_interval_h"],
                spot_taker_bps=_PAPER_EXCHANGE_SPEC["spot_taker_bps"],
                perp_taker_bps=_PAPER_EXCHANGE_SPEC["perp_taker_bps"],
            )
            session.add(exc)
            await session.flush()
        return exc.id

    def _db_pos_to_domain(self, row: DBPosition) -> Position:
        return Position(
            id=row.id,
            exchange_name=self.name,
            coin=row.coin,
            instrument=Instrument(row.instrument),
            side=Side(row.side),
            qty=row.qty,
            entry_price=row.entry_price,
            opened_at=_ms_to_dt(row.opened_at),
            closed_at=_ms_to_dt(row.closed_at) if row.closed_at is not None else None,
            status=PositionStatus(row.status),
            farb_position_id=row.farb_position_id,
        )

    async def _write_wallet_snapshot(
        self,
        session: AsyncSession,
        exchange_id: int,
        coin: str,
        ts_ms: int,
        balance: float,
        source: str,
    ) -> None:
        session.add(DBWalletSnapshot(
            exchange_id=exchange_id,
            coin=coin,
            ts_ms=ts_ms,
            balance=balance,
            source=source,
        ))

    # ------------------------------------------------------------------
    # Protocol: Delegated read methods
    # ------------------------------------------------------------------

    async def get_quote(self, coin: str) -> Quote:
        return await self._upstream.get_quote(coin)

    async def get_funding_rate(self, coin: str) -> FundingTick:
        return await self._upstream.get_funding_rate(coin)

    async def get_meta(self) -> list[MarketSpec]:
        return await self._upstream.get_meta()

    # ------------------------------------------------------------------
    # Protocol: open_position
    # ------------------------------------------------------------------

    async def open_position(self, req: OpenRequest) -> Position:
        now = datetime.now(UTC)
        now_ms = _dt_to_ms(now)

        if req.instrument == Instrument.COLLATERAL:
            # Simulate transfer: update wallet_snapshots for both wallets
            async with session_scope(self._session_factory) as s:
                exchange_id = await self._get_or_create_exchange_id(s)
                # Deduct from spot wallet
                spot_bal = await self._read_wallet_balance(s, exchange_id, req.coin, "spot")
                perp_bal = await self._read_wallet_balance(s, exchange_id, req.coin, "perp")
                await self._write_wallet_snapshot(
                    s, exchange_id, req.coin, now_ms,
                    spot_bal - req.qty, "paper_transfer_spot",
                )
                await self._write_wallet_snapshot(
                    s, exchange_id, req.coin, now_ms,
                    perp_bal + req.qty, "paper_transfer_perp",
                )
                row = DBPosition(
                    exchange_id=exchange_id,
                    coin=req.coin,
                    instrument=Instrument.COLLATERAL.value,
                    side=Side.NONE.value,
                    qty=req.qty,
                    entry_price=1.0,
                    opened_at=now_ms,
                    closed_at=None,
                    status=PositionStatus.OPEN.value,
                    farb_position_id=req.farb_position_id,
                )
                s.add(row)
                await s.flush()
                pos = self._db_pos_to_domain(row)
            return pos

        # SPOT or PERP: get fresh quote and simulate fill
        quote = await self._upstream.get_quote(req.coin)
        half_spread_bps = max(0.0, (quote.ask - quote.bid) / quote.mark * 5000)

        if req.instrument == Instrument.SPOT:
            if req.side != Side.LONG:
                raise NotImplementedError("SPOT SHORT not supported on HL spot")
            fill_price = quote.ask * (1 + self._extra_slip_bps / 10000)
            fee = req.qty * fill_price * self._fee_bps_spot / 10000
        else:  # PERP
            if req.side == Side.LONG:
                fill_price = quote.mark * (1 + half_spread_bps / 10000 + self._extra_slip_bps / 10000)
            else:
                fill_price = quote.mark * (1 - half_spread_bps / 10000 - self._extra_slip_bps / 10000)
            fee = req.qty * fill_price * self._fee_bps_perp / 10000

        async with session_scope(self._session_factory) as s:
            exchange_id = await self._get_or_create_exchange_id(s)
            row = DBPosition(
                exchange_id=exchange_id,
                coin=req.coin,
                instrument=req.instrument.value,
                side=req.side.value,
                qty=req.qty,
                entry_price=fill_price,
                opened_at=now_ms,
                closed_at=None,
                status=PositionStatus.OPEN.value,
                farb_position_id=req.farb_position_id,
            )
            s.add(row)
            await s.flush()
            fill_row = DBFill(
                position_id=row.id,
                ts_ms=now_ms,
                side=req.side.value,
                qty=req.qty,
                price=fill_price,
                fee=fee,
                slippage_bps=self._extra_slip_bps,
                is_paper=True,
            )
            s.add(fill_row)
            pos = self._db_pos_to_domain(row)
        return pos

    # ------------------------------------------------------------------
    # Protocol: close_position
    # ------------------------------------------------------------------

    async def close_position(self, pos: Position) -> Position:
        now = datetime.now(UTC)
        now_ms = _dt_to_ms(now)

        if pos.instrument == Instrument.COLLATERAL:
            async with session_scope(self._session_factory) as s:
                exchange_id = await self._get_or_create_exchange_id(s)
                # Simulate transfer back
                spot_bal = await self._read_wallet_balance(s, exchange_id, pos.coin, "spot")
                perp_bal = await self._read_wallet_balance(s, exchange_id, pos.coin, "perp")
                await self._write_wallet_snapshot(
                    s, exchange_id, pos.coin, now_ms,
                    perp_bal - pos.qty, "paper_transfer_perp",
                )
                await self._write_wallet_snapshot(
                    s, exchange_id, pos.coin, now_ms,
                    spot_bal + pos.qty, "paper_transfer_spot",
                )
                row = await s.get(DBPosition, pos.id)
                if row is None:
                    raise RuntimeError(f"Position {pos.id} not found in DB")
                row.status = PositionStatus.CLOSED.value
                row.closed_at = now_ms
                closed_pos = self._db_pos_to_domain(row)
            return closed_pos

        # SPOT or PERP: closing fill
        quote = await self._upstream.get_quote(pos.coin)
        half_spread_bps = max(0.0, (quote.ask - quote.bid) / quote.mark * 5000)

        if pos.instrument == Instrument.SPOT:
            # Closing SPOT LONG = selling
            fill_price = quote.bid * (1 - self._extra_slip_bps / 10000)
            fee = pos.qty * fill_price * self._fee_bps_spot / 10000
            closing_side = Side.SHORT  # SELL direction
        else:  # PERP
            # Closing LONG = sell (SHORT), closing SHORT = buy (LONG)
            if pos.side == Side.LONG:
                fill_price = quote.mark * (1 - half_spread_bps / 10000 - self._extra_slip_bps / 10000)
                closing_side = Side.SHORT
            else:
                fill_price = quote.mark * (1 + half_spread_bps / 10000 + self._extra_slip_bps / 10000)
                closing_side = Side.LONG
            fee = pos.qty * fill_price * self._fee_bps_perp / 10000

        async with session_scope(self._session_factory) as s:
            row = await s.get(DBPosition, pos.id)
            if row is None:
                raise RuntimeError(f"Position {pos.id} not found in DB")
            row.status = PositionStatus.CLOSED.value
            row.closed_at = now_ms
            fill_row = DBFill(
                position_id=row.id,
                ts_ms=now_ms,
                side=closing_side.value,
                qty=pos.qty,
                price=fill_price,
                fee=fee,
                slippage_bps=self._extra_slip_bps,
                is_paper=True,
            )
            s.add(fill_row)
            closed_pos = self._db_pos_to_domain(row)
        return closed_pos

    # ------------------------------------------------------------------
    # Protocol: get_open_positions
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[Position]:
        """Return all OPEN positions from DB (no upstream call for paper)."""
        async with session_scope(self._session_factory) as s:
            exchange_id = await self._get_or_create_exchange_id(s)
            result = await s.execute(
                select(DBPosition).where(
                    DBPosition.exchange_id == exchange_id,
                    DBPosition.status == PositionStatus.OPEN.value,
                )
            )
            rows = result.scalars().all()
        return [self._db_pos_to_domain(row) for row in rows]

    # ------------------------------------------------------------------
    # Protocol: get_accrued_funding
    # ------------------------------------------------------------------

    async def get_accrued_funding(self, pos: Position) -> float:
        """Compute funding from funding_rates table since pos.opened_at.

        For PERP SHORT: positive rate → accrual is positive (collected).
        For PERP LONG: positive rate → accrual is negative (paid).
        SPOT and COLLATERAL: return 0.0.

        Idempotent on (position_id, ts_ms).
        """
        if pos.id is None:
            raise ValueError("Position must have a DB id")
        if pos.instrument != Instrument.PERP:
            return 0.0

        since_ms = _dt_to_ms(pos.opened_at)
        now_ms = _dt_to_ms(datetime.now(UTC))

        async with session_scope(self._session_factory) as s:
            exchange_id = await self._get_or_create_exchange_id(s)
            result = await s.execute(
                select(DBFundingRate).where(
                    DBFundingRate.exchange_id == exchange_id,
                    DBFundingRate.coin == pos.coin,
                    DBFundingRate.ts_ms >= since_ms,
                    DBFundingRate.ts_ms <= now_ms,
                )
            )
            funding_rows = result.scalars().all()

            # Load existing accrual ts_ms values for idempotency
            existing_result = await s.execute(
                select(DBFundingAccrual.ts_ms).where(
                    DBFundingAccrual.position_id == pos.id
                )
            )
            existing_ts = {row for (row,) in existing_result.all()}

            # sign: SHORT collects positive funding, LONG pays
            sign = 1.0 if pos.side == Side.SHORT else -1.0

            for fr in funding_rows:
                if fr.ts_ms in existing_ts:
                    continue
                # accrual = qty * rate * sign (simplified; mark at that tick not stored)
                # We use the rate directly; in a full impl we'd use mark price from prices table
                amount = pos.qty * fr.rate * sign
                s.add(DBFundingAccrual(
                    position_id=pos.id,
                    ts_ms=fr.ts_ms,
                    amount=amount,
                ))

        # Return cumulative sum from DB
        async with session_scope(self._session_factory) as s:
            result = await s.execute(
                select(DBFundingAccrual.amount).where(
                    DBFundingAccrual.position_id == pos.id
                )
            )
            total = sum(row for (row,) in result.all())

        return total

    # ------------------------------------------------------------------
    # Protocol: get_wallet
    # ------------------------------------------------------------------

    async def get_wallet(self, coin: str, kind: WalletKind) -> float:
        """Return latest wallet_snapshots balance for (paper exchange, coin, kind).

        Returns 0.0 if no snapshot exists.
        """
        source_suffix = kind.value
        async with session_scope(self._session_factory) as s:
            exchange_id = await self._get_or_create_exchange_id(s)
            result = await s.execute(
                select(DBWalletSnapshot)
                .where(
                    DBWalletSnapshot.exchange_id == exchange_id,
                    DBWalletSnapshot.coin == coin,
                    DBWalletSnapshot.source.like(f"%{source_suffix}%"),
                )
                .order_by(DBWalletSnapshot.ts_ms.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        return row.balance if row is not None else 0.0

    # ------------------------------------------------------------------
    # Protocol: transfer
    # ------------------------------------------------------------------

    async def transfer(
        self,
        coin: str,
        amount: float,
        from_wallet: WalletKind,
        to_wallet: WalletKind,
    ) -> None:
        """Simulate transfer by updating wallet_snapshots."""
        if amount <= 0:
            raise ValueError(f"amount must be positive, got {amount!r}")
        now_ms = _dt_to_ms(datetime.now(UTC))
        async with session_scope(self._session_factory) as s:
            exchange_id = await self._get_or_create_exchange_id(s)
            from_bal = await self._read_wallet_balance(s, exchange_id, coin, from_wallet.value)
            to_bal = await self._read_wallet_balance(s, exchange_id, coin, to_wallet.value)
            await self._write_wallet_snapshot(
                s, exchange_id, coin, now_ms,
                from_bal - amount, f"paper_transfer_{from_wallet.value}",
            )
            await self._write_wallet_snapshot(
                s, exchange_id, coin, now_ms,
                to_bal + amount, f"paper_transfer_{to_wallet.value}",
            )

    # ------------------------------------------------------------------
    # Internal: wallet balance helper
    # ------------------------------------------------------------------

    async def _read_wallet_balance(
        self,
        session: AsyncSession,
        exchange_id: int,
        coin: str,
        kind_suffix: str,
    ) -> float:
        """Read latest balance for a wallet kind from DB within an existing session."""
        result = await session.execute(
            select(DBWalletSnapshot)
            .where(
                DBWalletSnapshot.exchange_id == exchange_id,
                DBWalletSnapshot.coin == coin,
                DBWalletSnapshot.source.like(f"%{kind_suffix}%"),
            )
            .order_by(DBWalletSnapshot.ts_ms.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row.balance if row is not None else 0.0
