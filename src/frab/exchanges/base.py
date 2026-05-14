from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Leg(StrEnum):
    SPOT = "spot"
    PERP = "perp"


class OrderType(StrEnum):
    MARKET = "market"


@dataclass(frozen=True, slots=True)
class Quote:
    coin: str
    ts_ms: int
    bid: float
    ask: float
    mark: float
    spot: float | None  # None if exchange has no spot for this coin


@dataclass(frozen=True, slots=True)
class FundingTick:
    coin: str
    ts_ms: int
    rate: float            # per-funding-period rate (e.g. hourly for HL)
    premium: float | None  # exchange-provided premium index if available
    annualized_pct: float  # rate * periods_per_year * 100, computed by caller


@dataclass(frozen=True, slots=True)
class MarketSpec:
    coin: str
    has_spot: bool
    has_perp: bool
    min_size: float
    tick_size: float


@dataclass(frozen=True, slots=True)
class OrderRequest:
    coin: str
    leg: Leg
    side: Side
    qty: float            # base units, positive
    order_type: OrderType = OrderType.MARKET
    client_ref: str | None = None  # for idempotency/traceability


@dataclass(frozen=True, slots=True)
class FillReport:
    coin: str
    leg: Leg
    side: Side
    ts_ms: int
    qty: float
    price: float
    fee: float
    slippage_bps: float
    is_paper: bool
    client_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PositionState:
    coin: str
    spot_units: float    # 0 if no spot leg
    perp_units: float    # negative for short
    avg_entry_spot: float | None
    avg_entry_perp: float | None


@runtime_checkable
class MarketDataSource(Protocol):
    name: str  # short identifier, e.g. "hyperliquid"

    async def fetch_funding(self, coin: str) -> FundingTick: ...  # pragma: no cover
    async def fetch_funding_history(self, coin: str, since_ms: int) -> list[FundingTick]: ...  # pragma: no cover
    async def fetch_quote(self, coin: str) -> Quote: ...  # pragma: no cover
    async def fetch_meta(self) -> list[MarketSpec]: ...  # pragma: no cover


@runtime_checkable
class Executor(Protocol):
    async def submit(self, req: OrderRequest) -> FillReport: ...  # pragma: no cover
    async def get_position(self, coin: str) -> PositionState | None: ...  # pragma: no cover
    async def reconcile(self) -> None: ...  # pragma: no cover
