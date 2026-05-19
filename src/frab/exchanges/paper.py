from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from frab.exchanges.base import (
    FillReport,
    Leg,
    MarketDataSource,
    OrderRequest,
    PositionState,
    Quote,
    Side,
)


@dataclass
class _PositionEntry:
    spot_units: float = 0.0
    perp_units: float = 0.0
    avg_entry_spot: float | None = None
    avg_entry_perp: float | None = None


def _update_leg(
    current_units: float,
    current_avg: float | None,
    qty_delta: float,
    price: float,
) -> tuple[float, float | None]:
    """Return (new_units, new_avg) after applying qty_delta at price."""
    if current_units == 0 or current_avg is None:
        new_units = qty_delta
        if abs(new_units) < 1e-12:
            return 0.0, None
        return new_units, price

    new_units = current_units + qty_delta
    if abs(new_units) < 1e-12:
        return 0.0, None

    # Same direction: weighted average
    if (current_units > 0 and qty_delta > 0) or (current_units < 0 and qty_delta < 0):
        new_avg = (abs(current_units) * current_avg + abs(qty_delta) * price) / abs(new_units)
        return new_units, new_avg

    # Opposite sign — check for flip
    if abs(qty_delta) > abs(current_units):
        raise ValueError(
            f"flip not supported: current_units={current_units}, qty_delta={qty_delta}"
        )

    # Reducing: keep avg
    return new_units, current_avg


class PaperExecutor:
    name = "paper"

    def __init__(
        self,
        market_data: MarketDataSource,
        spot_taker_bps: float,
        perp_taker_bps: float,
        extra_slip_bps: float = 2.0,
        clock_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._market_data = market_data
        self._spot_taker_bps = spot_taker_bps
        self._perp_taker_bps = perp_taker_bps
        self._extra_slip_bps = extra_slip_bps
        self._clock_fn = clock_fn if clock_fn is not None else lambda: datetime.now(UTC)
        self._positions: dict[str, _PositionEntry] = {}

    async def submit(self, req: OrderRequest) -> FillReport:
        quote = await self._market_data.fetch_quote(req.coin)
        slip = self._extra_slip_bps / 1e4

        spot_ask = quote.spot if quote.spot is not None else quote.ask
        spot_bid = quote.spot if quote.spot is not None else quote.bid

        if req.leg == Leg.SPOT:
            fee_bps = self._spot_taker_bps
            if req.side == Side.BUY:
                price = spot_ask * (1 + slip)
            else:
                price = spot_bid * (1 - slip)
        else:  # PERP
            fee_bps = self._perp_taker_bps
            if req.side == Side.BUY:
                price = quote.ask * (1 + slip)
            else:
                price = quote.bid * (1 - slip)

        fee = req.qty * price * fee_bps / 1e4
        qty_delta = req.qty if req.side == Side.BUY else -req.qty

        entry = self._positions.setdefault(req.coin, _PositionEntry())

        if req.leg == Leg.SPOT:
            new_units, new_avg = _update_leg(entry.spot_units, entry.avg_entry_spot, qty_delta, price)
            entry.spot_units = new_units
            entry.avg_entry_spot = new_avg
        else:
            new_units, new_avg = _update_leg(entry.perp_units, entry.avg_entry_perp, qty_delta, price)
            entry.perp_units = new_units
            entry.avg_entry_perp = new_avg

        return FillReport(
            coin=req.coin,
            leg=req.leg,
            side=req.side,
            ts=self._clock_fn(),
            qty=req.qty,
            price=price,
            fee=fee,
            slippage_bps=self._extra_slip_bps,
            is_paper=True,
            client_ref=req.client_ref,
        )

    async def get_position(self, coin: str) -> PositionState | None:
        entry = self._positions.get(coin)
        if entry is None or (abs(entry.spot_units) < 1e-12 and abs(entry.perp_units) < 1e-12):
            return None
        return PositionState(
            coin=coin,
            spot_units=entry.spot_units,
            perp_units=entry.perp_units,
            avg_entry_spot=entry.avg_entry_spot,
            avg_entry_perp=entry.avg_entry_perp,
        )

    async def reconcile(self) -> None:
        return None

    async def round_qty(self, coin: str, qty: float) -> float:
        return qty

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float:
        return qty
