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
    perp_state: HLPerpState,  # noqa: ARG001 — kept for signature stability; see note below
    spot_state: HLSpotState,
    *,
    spot_coin: str,
    raw_coin: str,
) -> float:
    """Total USDC cash balance for the HL account.

    Under HL unified margin the perp positions are collateralized by the spot
    USDC balance (still reported in spot_state), so perp_state.account_value is
    NOT separate cash — adding it double-counts the collateral and pulls in
    unrealized PnL that is offset by spot-token marks in the delta-neutral book.
    The USDC cash on hand is therefore just the spot USDC total. (Verified
    2026-06-04 against net deposits and HL's reported account value.)
    """
    return find_spot_balance(spot_state, spot_coin=spot_coin, raw_coin=raw_coin)[0]


def compute_non_usdc_total(
    spot_state: HLSpotState,
    *,
    spot_coin: str,
    raw_coin: str,
) -> float:
    """Return spot total for a non-USDC coin."""
    return find_spot_balance(spot_state, spot_coin=spot_coin, raw_coin=raw_coin)[0]
