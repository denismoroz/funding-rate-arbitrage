from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """Per-coin market facts fetched from exchange on startup."""

    coin: str
    has_spot: bool
    has_perp: bool
    max_leverage: int
    maint_ratio: float
    min_size: float
    tick_size: float
    spot_taker_bps: float | None = None
    perp_taker_bps: float | None = None
