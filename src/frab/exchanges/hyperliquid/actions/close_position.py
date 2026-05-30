"""ClosePositionAction: closes a SPOT (with retry-to-drain), PERP, or COLLATERAL position."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.db.models import Fill as DBFill, Position as DBPosition
from frab.db.session import session_scope
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.actions._base import HLAction, HLActionContext
from frab.exchanges.hyperliquid.actions._fees import fetch_real_fee_usdc
from frab.exchanges.hyperliquid.wire import HLOrderStatus

logger = logging.getLogger(__name__)

MIN_SPOT_RESIDUE_NOTIONAL_USDC = 11.0
MAX_CLOSE_RETRIES = 3
CLOSE_RETRY_SLIPPAGE_MULTIPLIER = 2.0


class ClosePositionAction(HLAction):
    requires_session = True

    def __init__(self, ctx: HLActionContext) -> None:
        super().__init__(ctx)
        self._client = ctx.client
        self._symbols = ctx.symbols
        self._sf = ctx.session_factory
        self._exchange_name = ctx.exchange_name
        self._address = ctx.address
        self._slippage = ctx.slippage
        self._clock_fn = ctx.clock_fn

    async def execute(self, pos: Position) -> Position:
        now_ms = int(self._clock_fn().timestamp() * 1000)

        if pos.instrument == Instrument.COLLATERAL:
            return await self._close_collateral(pos, now_ms)
        if pos.instrument == Instrument.SPOT:
            return await self._close_spot(pos, now_ms)
        return await self._close_perp(pos, now_ms)

    async def _close_collateral(self, pos: Position, now_ms: int) -> Position:
        async with session_scope(self._sf) as s:
            row = await s.get(DBPosition, pos.id)
            if row is None:
                raise RuntimeError(f"Position {pos.id} not found in DB")
            row.status = PositionStatus.CLOSED.value
            row.closed_at = now_ms
            return _row_to_domain(row, exchange_name=self._exchange_name)

    async def _close_spot(self, pos: Position, now_ms: int) -> Position:
        spot_name = self._symbols.make_spot_name(pos.coin)

        # Determine target qty: use actual HL balance to sweep accumulated dust.
        raw_balance = 0.0
        if self._address is not None:
            hl_coin = self._symbols.spot_token_map.get(pos.coin, pos.coin)
            state = await self._client.spot_user_state(self._address)
            match = next((b for b in state.balances if b.coin == hl_coin), None)
            raw_balance = float(match.total) if match is not None else 0.0
            rounded = await self._symbols.round_qty(pos.coin, raw_balance)
            target_qty = max(pos.qty, rounded)
            if target_qty != pos.qty:
                logger.info(
                    "close_spot %s sweeping dust: pos.qty=%s hl_balance=%s target=%s",
                    pos.coin, pos.qty, raw_balance, target_qty,
                )
        else:
            target_qty = pos.qty

        qty_filled_total = 0.0
        fee_total = 0.0
        fill_parts: list[tuple[float, float]] = []
        remaining_qty = target_qty
        current_slippage = self._slippage
        residue_notional = 0.0
        remaining = target_qty
        attempts = 0

        for attempt in range(MAX_CLOSE_RETRIES):
            attempts = attempt + 1
            order_resp = await self._client.market_open(spot_name, False, remaining_qty, current_slippage)
            status0 = order_resp.first
            qty_filled_i, fill_price_i, fee_i = await self._parse_close_fill(
                status0, taker=SPOT_TAKER, now_ms=now_ms,
            )

            qty_filled_total += qty_filled_i
            fee_total += fee_i
            fill_parts.append((qty_filled_i, fill_price_i))

            remaining = target_qty - qty_filled_total
            residue_notional = remaining * fill_price_i

            if remaining <= 0 or residue_notional < MIN_SPOT_RESIDUE_NOTIONAL_USDC:
                break

            current_slippage *= CLOSE_RETRY_SLIPPAGE_MULTIPLIER
            remaining_qty = await self._symbols.round_qty(pos.coin, remaining)
            if remaining_qty <= 0:
                break

        if qty_filled_total <= 0:
            raise RuntimeError(f"close_position drained 0 of {pos.qty} {pos.coin}")

        if residue_notional >= MIN_SPOT_RESIDUE_NOTIONAL_USDC:
            logger.warning(
                "close_position left %s residue of %s after %d attempts (notional=$%.2f)",
                remaining, pos.coin, attempts, residue_notional,
            )

        vwap_price = sum(q * p for q, p in fill_parts) / qty_filled_total
        closing_side = Side.SHORT if pos.side == Side.LONG else Side.LONG

        return await self._write_close(
            pos=pos, now_ms=now_ms,
            qty=qty_filled_total, price=vwap_price, fee=fee_total,
            side=closing_side, slippage=current_slippage,
        )

    async def _close_perp(self, pos: Position, now_ms: int) -> Position:
        order_resp = await self._client.market_close(pos.coin, self._slippage)
        status0 = order_resp.first
        qty_filled, fill_price, fee = await self._parse_close_fill(
            status0, taker=PERP_TAKER, now_ms=now_ms,
        )
        closing_side = Side.SHORT if pos.side == Side.LONG else Side.LONG
        return await self._write_close(
            pos=pos, now_ms=now_ms,
            qty=qty_filled, price=fill_price, fee=fee,
            side=closing_side, slippage=self._slippage,
        )

    async def _parse_close_fill(
        self, status: HLOrderStatus, *, taker: float, now_ms: int,
    ) -> tuple[float, float, float]:
        """Parse a close-side filled status; return (qty, price, fee). Raises on error/unrecognized."""
        if status.filled is None:
            if status.error is not None:
                raise RuntimeError(f"HL close error: {status.error!r}")
            raise RuntimeError(f"HL close unrecognized status: {status!r}")
        filled = status.filled
        qty_i = filled.qty
        price_i = filled.price
        oid_i = filled.oid
        if filled.fee_usdc is not None:
            return qty_i, price_i, filled.fee_usdc
        estimate = qty_i * price_i * taker
        real_fee: float | None = None
        if oid_i is not None and self._address is not None:
            real_fee = await fetch_real_fee_usdc(
                client=self._client, address=self._address,
                oid=oid_i, since_ms=now_ms - 5_000, clock_fn=self._clock_fn,
            )
        return qty_i, price_i, real_fee if real_fee is not None else estimate

    async def _write_close(
        self, *, pos: Position, now_ms: int,
        qty: float, price: float, fee: float,
        side: Side, slippage: float,
    ) -> Position:
        async with session_scope(self._sf) as s:
            row = await s.get(DBPosition, pos.id)
            if row is None:
                raise RuntimeError(f"Position {pos.id} not found in DB")
            row.status = PositionStatus.CLOSED.value
            row.closed_at = now_ms
            s.add(DBFill(
                position_id=row.id, ts_ms=now_ms, side=side.value,
                qty=qty, price=price, fee=fee,
                slippage_bps=slippage * 1e4, is_paper=False,
            ))
            return _row_to_domain(row, exchange_name=self._exchange_name)


def _row_to_domain(row: DBPosition, *, exchange_name: str) -> Position:
    return Position(
        id=row.id, exchange_name=exchange_name, coin=row.coin,
        instrument=Instrument(row.instrument), side=Side(row.side),
        qty=row.qty, entry_price=row.entry_price,
        opened_at=datetime.fromtimestamp(row.opened_at / 1000, tz=UTC),
        closed_at=datetime.fromtimestamp(row.closed_at / 1000, tz=UTC) if row.closed_at is not None else None,
        status=PositionStatus(row.status), farb_position_id=row.farb_position_id,
    )
