"""Tests for xsmom signal evaluator.

Key test: parity against research/cross_sectional/crypto/signals.py::momentum_ensemble.
"""
from __future__ import annotations

import sys
import os

import numpy as np
import pandas as pd
import pytest

# Make research signals importable without installing anything.
_RESEARCH_CRYPTO = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..", "..", "..", "..",  # src/frab/strategy/xsmom/evaluators/tests -> repo root
    "research", "cross_sectional", "crypto",
)
_RESEARCH_CRYPTO = os.path.normpath(_RESEARCH_CRYPTO)
if _RESEARCH_CRYPTO not in sys.path:
    sys.path.insert(0, _RESEARCH_CRYPTO)

from signals import momentum_ensemble  # noqa: E402  (research module)

from frab.strategy.xsmom.evaluators.signal import compute_scores  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

_DAY_MS = 86_400_000  # 1 day in ms
# Arbitrary epoch for test (2023-01-01 00:00:00 UTC in ms).
_T0_MS = 1_672_531_200_000


def _panel_to_closes(price_df: pd.DataFrame) -> dict[str, list[tuple[int, float]]]:
    """Convert DataFrame[date x coin] → {coin: [(day_ms, close), ...]} for compute_scores."""
    # Use integer timestamps: row i → T0_MS + i * DAY_MS.
    n = len(price_df)
    day_ms_arr = [_T0_MS + i * _DAY_MS for i in range(n)]
    result: dict[str, list[tuple[int, float]]] = {}
    for coin in price_df.columns:
        series = price_df[coin]
        rows = [
            (day_ms_arr[i], float(series.iloc[i]))
            for i in range(n)
            if pd.notna(series.iloc[i])
        ]
        if rows:
            result[coin] = rows
    return result


# ── Parity test ───────────────────────────────────────────────────────────────

def test_parity_against_research_momentum_ensemble():
    """compute_scores must produce IDENTICAL scores to research momentum_ensemble on the same panel."""
    rng = np.random.default_rng(42)
    n_days = 90
    coins = ["BTC", "ETH", "SOL", "AVAX", "DOGE", "ARB"]
    # Simulate cumulative log-returns as synthetic prices (all strictly positive).
    log_ret = rng.normal(0.0, 0.02, size=(n_days, len(coins)))
    price_arr = 1000.0 * np.exp(np.cumsum(log_ret, axis=0))
    price_df = pd.DataFrame(price_arr, columns=coins)

    # Reference: research momentum_ensemble on a plain price DataFrame.
    LOOKBACKS = (14, 21, 30, 45, 60)
    ref_ensemble = momentum_ensemble(price_df, lookbacks=LOOKBACKS)

    # Most recent row with at least one defined score (matches compute_scores).
    ref_latest_defined = ref_ensemble.dropna(how="all").iloc[-1]
    ref_scores = {
        coin: float(ref_latest_defined[coin])
        for coin in coins
        if pd.notna(ref_latest_defined[coin])
    }

    # Engine: convert panel to closes_by_coin form and call compute_scores.
    closes_by_coin = _panel_to_closes(price_df)
    engine_scores = compute_scores(closes_by_coin, lookbacks=LOOKBACKS)

    # Must return the same set of coins.
    assert set(engine_scores.keys()) == set(ref_scores.keys()), (
        f"coin sets differ: engine={set(engine_scores)}, ref={set(ref_scores)}"
    )

    # Each score must match to floating-point precision.
    for coin in ref_scores:
        assert np.isclose(engine_scores[coin], ref_scores[coin], rtol=1e-9, atol=1e-12), (
            f"{coin}: engine={engine_scores[coin]:.8f}, ref={ref_scores[coin]:.8f}"
        )


# ── Edge: insufficient history ────────────────────────────────────────────────

def test_coin_with_insufficient_history_is_omitted():
    """A coin with fewer than max(lookbacks)+1 data points must be omitted from results."""
    rng = np.random.default_rng(7)
    LOOKBACKS = (14, 21, 30, 45, 60)
    n_days = 90
    coins = ["BTC", "ETH", "NEW"]

    log_ret = rng.normal(0.0, 0.02, size=(n_days, 2))
    prices_btc_eth = 1000.0 * np.exp(np.cumsum(log_ret, axis=0))

    closes_by_coin: dict[str, list[tuple[int, float]]] = {
        "BTC": [((_T0_MS + i * _DAY_MS), float(prices_btc_eth[i, 0])) for i in range(n_days)],
        "ETH": [((_T0_MS + i * _DAY_MS), float(prices_btc_eth[i, 1])) for i in range(n_days)],
        # NEW has only 30 candles — not enough for lb=60.
        "NEW": [((_T0_MS + (n_days - 30 + i) * _DAY_MS), float(100.0 + i)) for i in range(30)],
    }

    scores = compute_scores(closes_by_coin, lookbacks=LOOKBACKS)

    assert "NEW" not in scores, "NEW has insufficient history; must be omitted"
    # BTC and ETH have full history and should be present.
    assert "BTC" in scores
    assert "ETH" in scores


# ── Edge: single coin → std==0 cross-sectionally ─────────────────────────────

def test_single_coin_returns_empty():
    """With only one coin, cross-sectional std=0 → NaN for all lookbacks → empty result."""
    n_days = 90
    closes: dict[str, list[tuple[int, float]]] = {
        "BTC": [((_T0_MS + i * _DAY_MS), float(100.0 + i)) for i in range(n_days)],
    }
    scores = compute_scores(closes, lookbacks=(14, 21, 30, 45, 60))
    assert scores == {}, f"single-coin must produce empty scores, got {scores}"


# ── Edge: two coins where one has zero cross-sectional spread ────────────────

def test_two_coins_identical_prices_returns_empty():
    """When two coins have identical prices, z-score std=0 every day → all NaN → empty."""
    n_days = 90
    prices = [(100.0 + i * 0.5) for i in range(n_days)]
    closes: dict[str, list[tuple[int, float]]] = {
        "A": [((_T0_MS + i * _DAY_MS), prices[i]) for i in range(n_days)],
        "B": [((_T0_MS + i * _DAY_MS), prices[i]) for i in range(n_days)],
    }
    scores = compute_scores(closes, lookbacks=(14, 21, 30, 45, 60))
    assert scores == {}, f"identical prices → std=0 → empty scores, got {scores}"


# ── Edge: empty input ─────────────────────────────────────────────────────────

def test_empty_input_returns_empty():
    assert compute_scores({}) == {}
    assert compute_scores({"BTC": []}) == {}


# ── Edge: no lookbacks ────────────────────────────────────────────────────────

def test_no_lookbacks_returns_empty():
    closes = {"BTC": [(_T0_MS, 100.0), (_T0_MS + _DAY_MS, 101.0)]}
    assert compute_scores(closes, lookbacks=()) == {}
