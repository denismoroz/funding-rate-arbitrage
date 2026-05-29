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

    async def get_open_positions(self) -> list[Position]: ...

    async def get_accrued_funding(self, pos: Position) -> float: ...

    async def get_wallet(self, coin: str, kind: WalletKind) -> float: ...

    async def transfer(
        self,
        coin: str,
        amount: float,
        from_wallet: WalletKind,
        to_wallet: WalletKind,
    ) -> None: ...
