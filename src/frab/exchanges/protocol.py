"""Exchange Protocol and DTOs for the new stateless exchange design.

Every Exchange implementation must satisfy this Protocol. No instance-level
caches of positions, wallet balances, or fills. Each method touches either
the upstream API or the DB.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from frab.domain import Instrument, Position, Side


@dataclass(frozen=True)
class Quote:
    coin: str
    mark: float          # mid mark
    spot: float | None   # spot mid if available
    bid: float
    ask: float
    ts_ms: int


@dataclass(frozen=True)
class FundingTick:
    coin: str
    ts_ms: int
    rate: float          # per-interval rate (decimal, e.g. 0.0001 = 1bp/interval)
    premium: float
    annualized_pct: float


@dataclass(frozen=True)
class MarketSpec:
    coin: str
    has_spot: bool
    has_perp: bool
    min_size: float
    tick_size: float
    sz_decimals: int     # HL-style sz_decimals


@dataclass(frozen=True)
class OpenRequest:
    coin: str
    instrument: Instrument
    side: Side
    qty: float           # for COLLATERAL, qty is USDC amount; entry_price=1.0
    farb_position_id: int | None = None   # link if the open is part of a composite
    leverage: int | None = None   # PERP only: cross-margin leverage to set before the order
    # When HL fills less than requested beyond tolerance, the filled part is REAL and
    # sits on the exchange. accept_partial=True records it as the position and returns
    # normally; False raises PartialFillError (the caller must then reconcile itself).
    # Single-leg books (XSMOM) set True: a rejected-but-filled leg becomes an orphan
    # nobody closes. Composite delta-neutral books (FRAB) keep False — there a short
    # leg against a full spot leg is a broken hedge, not a smaller position.
    accept_partial: bool = False


@dataclass(frozen=True)
class CloseCheck:
    """Whether a FarbPosition's legs can be closed within the venue's limits right now.

    ok=False means the spot leg could not be sold (below the minimum order at the current
    price): closing the perp first would strand an unhedged spot long. `reason` says why."""
    ok: bool
    reason: str | None = None
    spot_qty: float = 0.0               # what the spot close could sell, floored at the lot
    spot_price: float = 0.0             # current spot price the sale was valued at
    spot_notional_usdc: float = 0.0
    spot_min_notional_usdc: float = 0.0  # the bar the sale must clear
    wallet_qty: float = 0.0
    reserved_qty: float = 0.0           # coins of other open legs, never sold
    lot: float = 0.0


class WalletKind(str, Enum):
    SPOT = "spot"
    PERP = "perp"


@runtime_checkable
class Exchange(Protocol):
    name: str

    async def get_quote(self, coin: str) -> Quote: ...
    async def get_funding_rate(self, coin: str) -> FundingTick: ...
    async def get_meta(self) -> list[MarketSpec]: ...

    async def open_position(self, req: OpenRequest) -> Position: ...
    async def close_position(self, pos: Position) -> Position: ...
    async def check_close(self, spot: Position | None, perp: Position | None) -> CloseCheck: ...

    async def get_open_positions(self) -> list[Position]: ...

    async def get_accrued_funding(self, pos: Position, *, full: bool = False) -> float: ...

    async def get_wallet(self, coin: str, kind: WalletKind) -> float: ...

    async def transfer(
        self,
        coin: str,
        amount: float,
        from_wallet: WalletKind,
        to_wallet: WalletKind,
    ) -> None: ...

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float: ...

    async def get_daily_candles(self, coin: str, days: int) -> list[tuple[int, float]]: ...
