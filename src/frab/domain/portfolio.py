from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from frab.domain.exchange import Exchange
from frab.domain.position import Position
from frab.domain.wallet import WalletInfo


@dataclass(frozen=True, slots=True)
class Equity:
    """Point-in-time equity breakdown."""

    ts: datetime
    total_equity: float
    cash: float
    spot_value: float
    perp_unrealized: float
    perp_realized_cum: float
    funding_cum: float
    fees_cum: float


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Immutable snapshot used within one tick."""

    ts: datetime
    positions: tuple[Position, ...]
    wallet_per_exchange: dict[Exchange, WalletInfo]
    fees_cum: float
    funding_cum: float
    realized_pnl_cum: float

    def position(self, exchange: Exchange, coin: str) -> Position | None:
        """Return first open position matching exchange+coin, or None."""
        for pos in self.positions:
            if pos.exchange == exchange and pos.coin == coin:
                return pos
        return None

    def open_coins(self, exchange: Exchange) -> list[str]:
        """Coins with open positions on the given exchange (order preserved)."""
        return [p.coin for p in self.positions if p.exchange == exchange]

    def total_committed(self, exchange: Exchange) -> float:
        """Sum of notional_usd + margin_reserve_usd for all positions on exchange."""
        return sum(
            p.notional_usd + p.margin_reserve_usd
            for p in self.positions
            if p.exchange == exchange
        )

    def equity(self, marks: dict[tuple[Exchange, str], float]) -> Equity:
        """Compute equity snapshot at given mark prices.

        cash (free) + spot MTM + perp unrealized + reserved margin
        + cumulative realized + cumulative funding - cumulative fees
        """
        cash = sum(
            w.available_usdc for w in self.wallet_per_exchange.values()
        )
        spot_value = sum(
            p.spot_qty * marks[(p.exchange, p.coin)] for p in self.positions
        )
        perp_unrealized = sum(
            (p.entry_perp_price - marks[(p.exchange, p.coin)]) * p.perp_qty
            for p in self.positions
        )
        margin_reserved = sum(p.margin_reserve_usd for p in self.positions)
        total_equity = (
            cash
            + spot_value
            + perp_unrealized
            + margin_reserved
            + self.realized_pnl_cum
            + self.funding_cum
            - self.fees_cum
        )
        return Equity(
            ts=self.ts,
            total_equity=total_equity,
            cash=cash,
            spot_value=spot_value,
            perp_unrealized=perp_unrealized,
            perp_realized_cum=self.realized_pnl_cum,
            funding_cum=self.funding_cum,
            fees_cum=self.fees_cum,
        )
