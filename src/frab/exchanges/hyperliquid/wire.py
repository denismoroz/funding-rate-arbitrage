"""Typed wire dataclasses for Hyperliquid JSON shapes.

All classes are frozen dataclasses. No business logic; callers apply sign
conventions, filtering, and normalization on top of these raw shapes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HLFillRecord:
    qty: float          # from totalSz
    price: float        # from avgPx
    oid: int | None     # from filled.oid; None if missing
    fee_usdc: float | None  # from filled.fee; None if field missing — caller fallback


@dataclass(frozen=True)
class HLOrderStatus:
    """Discriminated union of HL's 3 status branches."""
    filled: HLFillRecord | None = None
    error: str | None = None
    resting_oid: int | None = None


@dataclass(frozen=True)
class HLOrderResponse:
    statuses: list[HLOrderStatus]

    @property
    def first(self) -> "HLOrderStatus":
        return self.statuses[0]


@dataclass(frozen=True)
class HLUserFill:
    oid: int
    side: str            # "B" buy / "A" sell
    sz: float
    px: float
    ts_ms: int           # from f["time"]
    fee_raw: float
    fee_token: str       # "USDC", "UBTC", etc
    coin: str            # raw HL coin string (e.g. "BTC", "@142", "UBTC/USDC")


@dataclass(frozen=True)
class HLPerpAssetPosition:
    coin: str
    szi: float                     # signed size (>0 long, <0 short)
    unrealized_pnl: float
    cum_funding_since_open: float  # raw HL value (negative when received); caller decides sign
    margin_used: float = 0.0       # HL position.marginUsed
    position_value: float = 0.0    # HL position.positionValue (notional in USDC)
    leverage_value: int | None = None  # HL position.leverage.value; None when missing


@dataclass(frozen=True)
class HLPerpState:
    account_value: float
    asset_positions: list[HLPerpAssetPosition]


@dataclass(frozen=True)
class HLSpotBalance:
    coin: str    # HL wrapped name ("UBTC", "USDC", ...)
    total: float
    hold: float


@dataclass(frozen=True)
class HLSpotState:
    balances: list[HLSpotBalance]


@dataclass(frozen=True)
class HLFundingRecord:
    coin: str
    ts_ms: int
    rate: float
    premium: float


@dataclass(frozen=True)
class HLFundingDelta:
    """One userFunding entry (a settlement payment)."""
    coin: str
    ts_ms: int
    amount_usdc: float   # from delta.usdc (already in USDC)


@dataclass(frozen=True)
class HLL2Snapshot:
    bid: float
    ask: float
    ts_ms: int


@dataclass(frozen=True)
class HLSpotPair:
    index: int    # the @N
    name: str     # resolved "BASE/QUOTE" if both tokens known, else raw entry.name


@dataclass(frozen=True)
class HLSpotMeta:
    tokens: dict[int, str]     # token_index → token name
    pairs: list[HLSpotPair]


@dataclass(frozen=True)
class HLPerpMarketSpec:
    name: str
    sz_decimals: int


@dataclass(frozen=True)
class HLCandle:
    """One daily OHLCV candle from HL candleSnapshot."""
    coin: str
    open_ms: int    # candle open time (t)
    close_ms: int   # candle close time (T)
    close: float    # parsed from c
