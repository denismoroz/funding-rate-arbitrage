"""Exchange fee constants (taker rates as decimals)."""
from dataclasses import dataclass

# Hyperliquid taker fees, per leg
PERP_TAKER = 0.00035
SPOT_TAKER = 0.00070

# Research defaults — research/portfolio_margin.py (PER_COIN_LEVERAGE, DEFAULT_MAINT_RATIO)
RESEARCH_LEVERAGE: dict[str, int] = {
    "BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10, "ZEC": 10, "PURR": 3, "XPL": 10,
}
RESEARCH_MAINT_RATIO: dict[str, float] = {
    "BTC": 0.01, "ETH": 0.01, "SOL": 0.025, "HYPE": 0.025, "ZEC": 0.025, "PURR": 0.025, "XPL": 0.025,
}

# Fallback when coin not in research either (unknown new market)
FALLBACK_LEVERAGE: int = 3
FALLBACK_MAINT_RATIO: float = 0.05


@dataclass(frozen=True)
class CoinMarginSpec:
    leverage: int       # HL initial margin = notional / leverage
    maint_ratio: float  # HL maintenance threshold = notional × maint_ratio
