from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    ts: datetime
    bid: float
    ask: float
    mark: float
    spot: float | None  # None if exchange has no spot for this coin


@dataclass(frozen=True, slots=True)
class FundingTick:
    coin: str
    ts: datetime
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
    ts: datetime
    qty: float
    price: float
    fee: float
    slippage_bps: float
    client_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PositionState:
    coin: str
    spot_units: float    # 0 if no spot leg
    perp_units: float    # negative for short
    avg_entry_spot: float | None
    avg_entry_perp: float | None


@dataclass(frozen=True, slots=True)
class UserFill:
    coin: str          # normalized coin name (e.g. "BTC", not "UBTC/USDC")
    ts: datetime       # parsed from fill["time"] (ms since epoch, UTC)
    leg: Leg           # PERP or SPOT — inferred from HL coin field (slash → SPOT)
    side: Side         # BUY or SELL — HL "B" → BUY, "A" → SELL
    qty: float         # abs(fill["sz"])
    price: float       # float(fill["px"])
    fee: float         # float(fill["fee"]) — USDC for perp, asset units for spot BUY
    fee_token: str     # fill["feeToken"] e.g. "USDC", "UBTC"
    hl_oid: int        # fill["oid"] — HL order id
    hl_tid: int        # fill["tid"] — HL trade id, globally unique per fill


@dataclass(frozen=True, slots=True)
class FundingPayment:
    coin: str      # plain perp coin name, e.g. "BTC"
    ts: datetime   # from delta["time"], UTC-aware
    usdc: float    # signed payment amount in USDC (+ = received, - = paid)
    szi: float     # signed position size at event time
    rate: float    # fundingRate
    hash: str      # HL tx hash, useful for idempotency/dedup


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
    async def round_qty(self, coin: str, qty: float) -> float: ...  # pragma: no cover
    async def round_qty_to_nearest(self, coin: str, qty: float) -> float: ...  # pragma: no cover
    async def transfer_spot_to_perp(self, usdc_amount: float) -> dict: ...  # pragma: no cover
    async def transfer_perp_to_spot(self, usdc_amount: float) -> dict: ...  # pragma: no cover
