"""Exchange fee constants (taker rates as decimals)."""
from dataclasses import dataclass

# Hyperliquid taker fees, per leg
PERP_TAKER = 0.00035
SPOT_TAKER = 0.00070


@dataclass(frozen=True)
class CoinMarginSpec:
    leverage: int       # HL initial margin = notional / leverage
    maint_ratio: float  # HL maintenance threshold = notional × maint_ratio
