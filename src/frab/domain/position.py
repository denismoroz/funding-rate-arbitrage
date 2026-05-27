from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from frab.domain.exchange import Exchange


@dataclass(frozen=True, slots=True)
class Position:
    """One delta-neutral spot+perp pair on a single exchange."""

    exchange: Exchange
    coin: str
    spot_qty: float
    perp_qty: float
    notional_usd: float
    margin_reserve_usd: float
    entry_spot_price: float
    entry_perp_price: float
    opened_at: datetime
    funding_collected: float = 0.0
    fees_paid: float = 0.0
    state: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    """Result of close_position; carries realized PnL for portfolio update."""

    exchange: Exchange
    coin: str
    closed_at: datetime
    realized_pnl: float
    fees_paid_total: float
    funding_collected_total: float
    released_margin_usd: float
    released_notional_usd: float = 0.0
