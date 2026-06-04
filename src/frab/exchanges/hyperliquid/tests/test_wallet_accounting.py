"""Unit tests for wallet_accounting pure functions."""
from __future__ import annotations

import pytest

from frab.exchanges.hyperliquid.actions.wallet_accounting import (
    compute_non_usdc_total,
    compute_total_usdc,
    find_spot_balance,
)
from frab.exchanges.hyperliquid.wire import (
    HLPerpAssetPosition,
    HLPerpState,
    HLSpotBalance,
    HLSpotState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spot_state(*entries: tuple[str, float, float]) -> HLSpotState:
    return HLSpotState(balances=[HLSpotBalance(coin=c, total=t, hold=h) for c, t, h in entries])


def _perp_state(
    account_value: float,
    *positions: tuple[str, float, float],  # (coin, unrealized_pnl, cum_funding_since_open)
) -> HLPerpState:
    return HLPerpState(
        account_value=account_value,
        asset_positions=[
            HLPerpAssetPosition(coin=c, szi=0.0, unrealized_pnl=u, cum_funding_since_open=f)
            for c, u, f in positions
        ],
    )


# ---------------------------------------------------------------------------
# find_spot_balance
# ---------------------------------------------------------------------------

def test_find_spot_balance_matches_by_spot_coin():
    state = _spot_state(("UBTC", 5.0, 1.0))
    total, hold = find_spot_balance(state, spot_coin="UBTC", raw_coin="BTC")
    assert total == 5.0
    assert hold == 1.0


def test_find_spot_balance_matches_by_raw_coin():
    # Balance entry is "BTC" (raw), spot_coin resolves to "UBTC" but entry uses canonical name
    state = _spot_state(("BTC", 10.0, 0.0))
    total, hold = find_spot_balance(state, spot_coin="UBTC", raw_coin="BTC")
    assert total == 10.0
    assert hold == 0.0


def test_find_spot_balance_returns_zero_when_no_match():
    state = _spot_state(("UETH", 3.0, 0.5))
    total, hold = find_spot_balance(state, spot_coin="UBTC", raw_coin="BTC")
    assert total == 0.0
    assert hold == 0.0


def test_find_spot_balance_skips_entries_with_different_coin():
    state = _spot_state(("UETH", 3.0, 0.0), ("USOL", 2.0, 0.0))
    total, hold = find_spot_balance(state, spot_coin="UBTC", raw_coin="BTC")
    assert total == 0.0
    assert hold == 0.0


def test_find_spot_balance_first_match_wins():
    # Two entries both matching: first one should be returned
    state = HLSpotState(balances=[
        HLSpotBalance(coin="UBTC", total=7.0, hold=1.0),
        HLSpotBalance(coin="BTC", total=99.0, hold=0.0),
    ])
    total, hold = find_spot_balance(state, spot_coin="UBTC", raw_coin="BTC")
    assert total == 7.0
    assert hold == 1.0


# ---------------------------------------------------------------------------
# compute_total_usdc
# ---------------------------------------------------------------------------

def test_compute_total_usdc_returns_spot_usdc_only():
    # Unified margin: perp account_value / unrealized / funding are NOT added —
    # the perp collateral is drawn from the spot USDC already reported here, so
    # USDC cash on hand == spot USDC total.
    perp = _perp_state(1000.0, ("BTC", 50.0, -5.0))
    spot = _spot_state(("USDC", 200.0, 50.0))
    result = compute_total_usdc(perp, spot, spot_coin="USDC", raw_coin="USDC")
    assert result == pytest.approx(200.0)


def test_compute_total_usdc_ignores_perp_account_value():
    # account_value=500 but no spot USDC → 0 (perp contributes nothing)
    perp = _perp_state(500.0)
    spot = _spot_state(("USDC", 300.0, 0.0))
    result = compute_total_usdc(perp, spot, spot_coin="USDC", raw_coin="USDC")
    assert result == pytest.approx(300.0)


def test_compute_total_usdc_no_spot_match_is_zero():
    # No matching spot balance → 0, regardless of perp positions/PnL
    perp = _perp_state(1000.0, ("BTC", 20.0, -2.0), ("ETH", 30.0, -3.0))
    spot = _spot_state()  # no spot match
    result = compute_total_usdc(perp, spot, spot_coin="USDC", raw_coin="USDC")
    assert result == pytest.approx(0.0)


def test_compute_total_usdc_raw_coin_fallback():
    # spot_coin="UBTC" but balance entry uses raw_coin="BTC"
    perp = _perp_state(500.0)
    spot = _spot_state(("BTC", 10.0, 0.0))
    result = compute_total_usdc(perp, spot, spot_coin="UBTC", raw_coin="BTC")
    assert result == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# compute_non_usdc_total
# ---------------------------------------------------------------------------

def test_compute_non_usdc_total_returns_spot_total():
    spot = _spot_state(("UBTC", 2.5, 0.1))
    result = compute_non_usdc_total(spot, spot_coin="UBTC", raw_coin="BTC")
    assert result == pytest.approx(2.5)


def test_compute_non_usdc_total_zero_when_no_match():
    spot = _spot_state(("UETH", 1.0, 0.0))
    result = compute_non_usdc_total(spot, spot_coin="UBTC", raw_coin="BTC")
    assert result == 0.0
