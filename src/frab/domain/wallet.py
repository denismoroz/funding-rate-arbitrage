from __future__ import annotations

from dataclasses import dataclass

from frab.domain.exchange import Exchange


@dataclass(frozen=True, slots=True)
class WalletInfo:
    """Snapshot of one exchange wallet."""

    exchange: Exchange
    available_usdc: float
    reserved_usdc: float
    total_value_usd: float
