"""Canonical total-equity definition — the SINGLE source of truth.

Every surface that reports account equity (the live /api/equity/summary
endpoint, the persisted EquitySnapshot that drives the dashboard Total + chart,
and the wallet total) must call `total_equity_usd`. Do NOT open-code an equity
sum anywhere else — historically there were three divergent formulas and they
drifted by ~$8-17.
"""
from __future__ import annotations


def total_equity_usd(cash_usdc: float, spot_tokens_value: float) -> float:
    """Total account equity in USD = spot USDC + spot tokens at mark.

    This matches Hyperliquid's reported account value (and net-deposits minus
    PnL). Under HL unified margin the perp positions are collateralized by the
    spot USDC already in `cash_usdc`, and in this delta-neutral book the perp
    unrealized PnL mirrors the spot-token marks. So equity is simply cash +
    spot tokens.

    NEVER add perp `account_value` or perp unrealized PnL on top — that
    double-counts (the live account overstated by ~$8 from unrealized and ~$17
    from account_value) and diverges from HL. Verified 2026-06-04 against HL's
    portfolio accountValue ($124.25) and net deposits ($129.16, PnL -$4.9).
    """
    return cash_usdc + spot_tokens_value
