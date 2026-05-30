"""OpenPositionAction: opens a SPOT, PERP, or COLLATERAL position on HL and writes to DB."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.db.models import Exchange as DBExchange, Fill as DBFill, Position as DBPosition
from frab.db.session import session_scope
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.actions._base import HLAction, HLActionContext
from frab.exchanges.hyperliquid.actions._fees import fetch_real_fee_usdc
from frab.exchanges.protocol import OpenRequest

logger = logging.getLogger(__name__)


class PartialFillError(RuntimeError):
    """Raised when HL filled less than the requested qty beyond tolerance."""

    def __init__(self, requested_qty: float, filled_qty: float, fill_price: float) -> None:
        super().__init__(
            f"partial fill: requested {requested_qty}, filled {filled_qty} "
            f"({filled_qty / requested_qty * 100:.1f}%)"
        )
        self.requested_qty = requested_qty
        self.filled_qty = filled_qty
        self.fill_price = fill_price


class OpenPositionAction(HLAction):
    """Open one position (COLLATERAL bookkeeping, SPOT order, or PERP order)."""

    requires_session = True

    def __init__(self, ctx: HLActionContext) -> None:
        super().__init__(ctx)
        self._client = ctx.client
        self._symbols = ctx.symbols
        self._sf = ctx.session_factory
        self._exchange_name = ctx.exchange_name
        self._address = ctx.address
        self._slippage = ctx.slippage
        self._partial_fill_tolerance = ctx.partial_fill_tolerance
        self._clock_fn = ctx.clock_fn

    async def execute(self, req: OpenRequest) -> Position:
        now_ms = int(self._clock_fn().timestamp() * 1000)

        if req.instrument == Instrument.COLLATERAL:
            return await self._open_collateral(req, now_ms)

        # SPOT or PERP
        wire_qty = await self._symbols.round_qty(req.coin, req.qty)
        if wire_qty <= 0:
            raise RuntimeError(
                f"qty {req.qty} rounds to 0 at szDecimals for coin={req.coin}"
            )

        if req.instrument == Instrument.SPOT:
            symbol = self._symbols.make_spot_name(req.coin)
        else:
            if req.leverage is not None:
                if req.leverage <= 0:
                    raise ValueError(f"leverage must be > 0, got {req.leverage!r}")
                await self._client.update_leverage(req.coin, req.leverage)
            symbol = req.coin

        is_buy = req.side == Side.LONG
        order_resp = await self._client.market_open(symbol, is_buy, wire_qty, self._slippage)
        status0 = order_resp.first

        if status0.filled is None:
            if status0.error is not None:
                raise RuntimeError(f"HL order error: {status0.error!r}")
            if status0.resting_oid is not None:
                raise RuntimeError(
                    f"HL market order unexpectedly resting: oid={status0.resting_oid!r}"
                )
            raise RuntimeError(f"HL order unrecognized status: {status0!r}")

        filled = status0.filled
        qty_filled = filled.qty
        fill_price = filled.price
        oid = filled.oid
        taker = SPOT_TAKER if req.instrument == Instrument.SPOT else PERP_TAKER

        if filled.fee_usdc is not None:
            fee = filled.fee_usdc
        else:
            estimate = qty_filled * fill_price * taker
            real_fee: float | None = None
            if oid is not None and self._address is not None:
                real_fee = await fetch_real_fee_usdc(
                    client=self._client,
                    address=self._address,
                    oid=oid,
                    since_ms=now_ms - 5_000,
                    clock_fn=self._clock_fn,
                )
            fee = real_fee if real_fee is not None else estimate

        if qty_filled < wire_qty * (1 - self._partial_fill_tolerance):
            raise PartialFillError(
                requested_qty=wire_qty, filled_qty=qty_filled, fill_price=fill_price
            )

        async with session_scope(self._sf) as s:
            exchange_id = await self._get_exchange_id(s)
            row = DBPosition(
                exchange_id=exchange_id,
                coin=req.coin,
                instrument=req.instrument.value,
                side=req.side.value,
                qty=qty_filled,
                entry_price=fill_price,
                opened_at=now_ms,
                closed_at=None,
                status=PositionStatus.OPEN.value,
                farb_position_id=req.farb_position_id,
            )
            s.add(row)
            await s.flush()
            s.add(DBFill(
                position_id=row.id,
                ts_ms=now_ms,
                side=req.side.value,
                qty=qty_filled,
                price=fill_price,
                fee=fee,
                slippage_bps=self._slippage * 1e4,
                is_paper=False,
            ))
            return _row_to_domain(row, exchange_name=self._exchange_name)

    async def _open_collateral(self, req: OpenRequest, now_ms: int) -> Position:
        async with session_scope(self._sf) as s:
            exchange_id = await self._get_exchange_id(s)
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
            return _row_to_domain(row, exchange_name=self._exchange_name)

    async def _get_exchange_id(self, session: AsyncSession) -> int:
        result = await session.execute(
            select(DBExchange).where(DBExchange.name == self._exchange_name)
        )
        exc = result.scalar_one_or_none()
        if exc is None:
            raise RuntimeError(
                f"Exchange {self._exchange_name!r} not found in DB; run `frab seed` first."
            )
        return exc.id


def _row_to_domain(row: DBPosition, *, exchange_name: str) -> Position:
    from datetime import UTC, datetime as _dt
    return Position(
        id=row.id,
        exchange_name=exchange_name,
        coin=row.coin,
        instrument=Instrument(row.instrument),
        side=Side(row.side),
        qty=row.qty,
        entry_price=row.entry_price,
        opened_at=_dt.fromtimestamp(row.opened_at / 1000, tz=UTC),
        closed_at=_dt.fromtimestamp(row.closed_at / 1000, tz=UTC) if row.closed_at is not None else None,
        status=PositionStatus(row.status),
        farb_position_id=row.farb_position_id,
    )
