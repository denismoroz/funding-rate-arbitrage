"""Pure functions for HL cross-margin USDC wallet math."""
from __future__ import annotations

from frab.exchanges.hyperliquid.wire import HLPerpState, HLSpotState


def find_spot_balance(
    spot_state: HLSpotState,
    *,
    spot_coin: str,
    raw_coin: str,
) -> tuple[float, float]:
    """Walk spot_state.balances; match by spot_coin or raw_coin. Returns (total, hold)."""
    for bal in spot_state.balances:
        if bal.coin == spot_coin or bal.coin == raw_coin:
            return (bal.total, bal.hold)
    return (0.0, 0.0)


def compute_total_usdc(
    perp_state: HLPerpState,
    spot_state: HLSpotState,
    *,
    spot_coin: str,
    raw_coin: str,
) -> float:
    """Compute cross-margin total USDC balance across perp and spot sub-wallets."""
    account_value = perp_state.account_value
    unrealized_total = sum(ap.unrealized_pnl for ap in perp_state.asset_positions)
    # HL sign convention: cumFunding.sinceOpen is negative when received (a credit).
    # Flip to "received" semantics.
    cum_funding_received = sum(-ap.cum_funding_since_open for ap in perp_state.asset_positions)
    spot_total, spot_hold = find_spot_balance(spot_state, spot_coin=spot_coin, raw_coin=raw_coin)
    perp_standalone = account_value - spot_hold - unrealized_total - cum_funding_received
    return spot_total + perp_standalone


def compute_non_usdc_total(
    spot_state: HLSpotState,
    *,
    spot_coin: str,
    raw_coin: str,
) -> float:
    """Return spot total for a non-USDC coin."""
    return find_spot_balance(spot_state, spot_coin=spot_coin, raw_coin=raw_coin)[0]
