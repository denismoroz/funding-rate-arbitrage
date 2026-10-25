"""ClosePositionAction: closes a SPOT (with retry-to-drain), PERP, or COLLATERAL position."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.db.models import Fill as DBFill, Position as DBPosition
from frab.db.session import session_scope
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.actions._base import HLAction, HLActionContext
from frab.exchanges.hyperliquid.actions._fees import fetch_real_fee_usdc
from frab.exchanges.hyperliquid.wire import HLOrderStatus
from frab.exchanges.protocol import CloseCheck

logger = logging.getLogger(__name__)

# HL rejects spot orders below $10; the retry loop does not place one below $11 so a price
# move between the check and the fill cannot turn a sellable remainder into a rejected order.
# The same $11 is the pre-close check's bar (`check_close`): a position whose spot cannot
# clear it now would be closed on the perp side and then stuck unhedged on the spot side.
MIN_SPOT_RESIDUE_NOTIONAL_USDC = 11.0
HL_MIN_ORDER_NOTIONAL_USDC = 10.0
MAX_CLOSE_RETRIES = 3
CLOSE_RETRY_SLIPPAGE_MULTIPLIER = 2.0


def sellable_spot_qty(
    *, wallet_qty: float, reserved_qty: float, budget: Decimal, sold_qty: float,
) -> Decimal:
    """Unfloored qty a close may sell: the wallet balance less the coins of other open
    legs, capped at what is left of this position's budget (its qty plus one lot).
    Never negative."""
    sellable = min(
        Decimal(str(wallet_qty)) - Decimal(str(reserved_qty)),
        budget - Decimal(str(sold_qty)),
    )
    return max(sellable, Decimal(0))


@dataclass(frozen=True)
class SpotSale:
    """What a spot close could sell right now, and whether the sale clears the bar."""
    wallet_qty: float
    reserved_qty: float
    lot: float
    qty: float            # sellable, floored at the spot lot
    price: float
    price_is_current: bool  # False: the live price was unavailable, the entry price stands in
    notional_usdc: float

    @property
    def ok(self) -> bool:
        return self.qty > 0 and self.notional_usdc >= MIN_SPOT_RESIDUE_NOTIONAL_USDC


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

    async def check_close(self, spot: Position | None, perp: Position | None) -> CloseCheck:
        """Can the legs of one FarbPosition be closed within HL's limits RIGHT NOW?

        Spot: what this position may sell (same budget as the close itself) at the CURRENT
        spot price must clear MIN_SPOT_RESIDUE_NOTIONAL_USDC. Perp: no check. The perp is
        closed with a reduce-only IOC order (SDK `market_close`), which HL exempts from the
        $10 minimum order value ("The only exception is exactly closing a position with a
        reduce-only order", Chainstack's HL order-precision guide), and prod has done it:
        XSMOM's WLD perp (fill 59, 25.3 x 0.36659 = $9.27) closed at that size.
        Read-only: places nothing. Raises when the wallet or the live price cannot be read.
        """
        if spot is None:
            return CloseCheck(ok=True)
        if self._address is None:
            # No live address, no wallet to read: the close falls back to a one-shot sale.
            return CloseCheck(ok=True)
        sale = await self.plan_spot_sale(spot)
        if not sale.price_is_current:
            # An entry price says nothing about today's order value: do not rule on it.
            raise RuntimeError(f"no live spot price for {spot.coin}; cannot value the sale")
        if sale.ok:
            return CloseCheck(
                ok=True, spot_qty=sale.qty, spot_price=sale.price,
                spot_notional_usdc=sale.notional_usdc,
                spot_min_notional_usdc=MIN_SPOT_RESIDUE_NOTIONAL_USDC,
                wallet_qty=sale.wallet_qty, reserved_qty=sale.reserved_qty, lot=sale.lot,
            )
        return CloseCheck(
            ok=False,
            reason=(
                f"spot {sale.qty:g} {spot.coin} x ${sale.price:.6g} = ${sale.notional_usdc:.2f} "
                f"< ${MIN_SPOT_RESIDUE_NOTIONAL_USDC:g} the close needs "
                f"(HL minimum order ${HL_MIN_ORDER_NOTIONAL_USDC:g})"
            ),
            spot_qty=sale.qty, spot_price=sale.price, spot_notional_usdc=sale.notional_usdc,
            spot_min_notional_usdc=MIN_SPOT_RESIDUE_NOTIONAL_USDC,
            wallet_qty=sale.wallet_qty, reserved_qty=sale.reserved_qty, lot=sale.lot,
        )

    async def plan_spot_sale(self, pos: Position) -> SpotSale:
        """The first sale a close of `pos` would make, valued at the current spot price."""
        if self._address is None:
            raise RuntimeError("plan_spot_sale needs a live address")
        hl_coin = self._symbols.spot_token_map.get(pos.coin, pos.coin)
        state = await self._client.spot_user_state(self._address)
        match = next((b for b in state.balances if b.coin == hl_coin), None)
        wallet_qty = float(match.total) if match is not None else 0.0
        reserved = await self._other_open_spot_qty(pos)
        lot = await self._symbols.lot_size(pos.coin)
        budget = Decimal(str(pos.qty)) + Decimal(str(lot))
        sellable = sellable_spot_qty(
            wallet_qty=wallet_qty, reserved_qty=reserved, budget=budget, sold_qty=0.0,
        )
        qty = await self._symbols.round_spot_qty(pos.coin, sellable)
        price, is_current = await self._current_spot_price(pos)
        return SpotSale(
            wallet_qty=wallet_qty, reserved_qty=reserved, lot=lot, qty=qty,
            price=price, price_is_current=is_current, notional_usdc=qty * price,
        )

    async def _current_spot_price(self, pos: Position) -> tuple[float, bool]:
        """(price, is_current): the live spot mid, else the entry price as a stand-in.

        The gate must judge the sale at today's price: a coin that fell since entry sells
        for less than entry x qty (PURR: entered at 0.14446, trading near 0.099)."""
        try:
            mids = await self._symbols.spot_mids_by_coin()
        except Exception as exc:  # noqa: BLE001 - a missing price must not stop a close
            logger.warning("current spot price of %s unavailable (%s)", pos.coin, exc)
            mids = {}
        mid = mids.get(pos.coin)
        if isinstance(mid, (int, float)) and mid > 0:
            return float(mid), True
        logger.warning("no live spot mid for %s; judging the sale at the entry price", pos.coin)
        return pos.entry_price, False

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

        if self._address is None:
            # No live address → cannot query wallet, fall back to one-shot close at pos.qty.
            order_resp = await self._client.market_open(spot_name, False, pos.qty, self._slippage)
            status0 = order_resp.first
            qty_filled, fill_price, fee = await self._parse_close_fill(
                status0, taker=SPOT_TAKER, now_ms=now_ms,
            )
            if qty_filled <= 0:
                raise RuntimeError(f"close_position drained 0 of {pos.qty} {pos.coin}")
            closing_side = Side.SHORT if pos.side == Side.LONG else Side.LONG
            return await self._write_close(
                pos=pos, now_ms=now_ms,
                qty=qty_filled, price=fill_price, fee=fee,
                side=closing_side, slippage=self._slippage,
            )

        hl_coin = self._symbols.spot_token_map.get(pos.coin, pos.coin)
        qty_filled_total = 0.0
        fee_total = 0.0
        fill_parts: list[tuple[float, float]] = []
        current_slippage = self._slippage
        # Price for the residue bar until a fill gives a better one: the CURRENT spot mid
        # (the entry price only when the mid is unavailable).
        last_price, _ = await self._current_spot_price(pos)
        attempts = 0
        final_balance = 0.0
        balance_after = 0.0  # wallet balance of the coin as of the last look, net of our last sale

        # What this close may sell. The wallet holds the coin of every open position of
        # the same coin plus the sub-lot remainders of earlier closes, and may hold coins
        # nobody here owns. This position's share is its recorded qty plus at most one lot
        # of such remainder (the next position absorbs remainders by design; they are always
        # below one lot). Everything else stays: it belongs to another open leg or to the owner.
        reserved = await self._other_open_spot_qty(pos)
        lot = await self._symbols.lot_size(pos.coin)
        budget = Decimal(str(pos.qty)) + Decimal(str(lot))

        for attempt in range(MAX_CLOSE_RETRIES):
            attempts = attempt + 1
            state = await self._client.spot_user_state(self._address)
            match = next((b for b in state.balances if b.coin == hl_coin), None)
            raw_balance = float(match.total) if match is not None else 0.0
            balance_after = raw_balance
            sellable = sellable_spot_qty(
                wallet_qty=raw_balance, reserved_qty=reserved, budget=budget,
                sold_qty=qty_filled_total,
            )
            # Floor at the SPOT token's lot, not the perp's: a sale is the only chance
            # to take the coins out, and what the floor leaves can never be sold.
            rounded = await self._symbols.round_spot_qty(pos.coin, sellable)
            residue_notional = rounded * last_price
            final_balance = float(sellable)

            if rounded <= 0 or residue_notional < MIN_SPOT_RESIDUE_NOTIONAL_USDC:
                break

            order_resp = await self._client.market_open(spot_name, False, rounded, current_slippage)
            status0 = order_resp.first
            qty_filled_i, fill_price_i, fee_i = await self._parse_close_fill(
                status0, taker=SPOT_TAKER, now_ms=now_ms,
            )
            qty_filled_total += qty_filled_i
            balance_after = raw_balance - qty_filled_i
            fee_total += fee_i
            fill_parts.append((qty_filled_i, fill_price_i))
            last_price = fill_price_i
            current_slippage *= CLOSE_RETRY_SLIPPAGE_MULTIPLIER

        if qty_filled_total <= 0:
            raise RuntimeError(f"close_position drained 0 of {pos.qty} {pos.coin}")

        logger.info(
            "close_position %s: sold %s, wallet keeps %s (reserved for other open legs: %s; "
            "one lot: %s)",
            pos.coin, qty_filled_total, balance_after, reserved, lot,
        )

        if final_balance * last_price >= MIN_SPOT_RESIDUE_NOTIONAL_USDC:
            logger.warning(
                "close_position left %s residue of %s after %d attempts (notional=$%.2f)",
                final_balance, pos.coin, attempts, final_balance * last_price,
            )

        vwap_price = sum(q * p for q, p in fill_parts) / qty_filled_total
        closing_side = Side.SHORT if pos.side == Side.LONG else Side.LONG

        return await self._write_close(
            pos=pos, now_ms=now_ms,
            qty=qty_filled_total, price=vwap_price, fee=fee_total,
            side=closing_side, slippage=current_slippage,
        )

    async def _other_open_spot_qty(self, pos: Position) -> float:
        """Recorded qty of the OTHER open spot positions of the same coin: tokens that sit
        in the same wallet balance but belong to a leg that is still open."""
        async with session_scope(self._sf) as s:
            total = (await s.execute(
                select(func.coalesce(func.sum(DBPosition.qty), 0.0)).where(
                    DBPosition.coin == pos.coin,
                    DBPosition.instrument == Instrument.SPOT.value,
                    DBPosition.status == PositionStatus.OPEN.value,
                    DBPosition.id != pos.id,
                )
            )).scalar_one()
        return float(total)

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
                spot_token_inverse=self._symbols.spot_token_inverse,
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
