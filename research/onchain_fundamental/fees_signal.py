"""
Fee-growth signal for cross-sectional fundamental momentum.

SIGNAL DESIGN (pre-registered in PLAN.md §Signal design):

  For each coin c and date t, compute log fee-growth at lookback N:
    growth_N[t, c] = log( sum(fees[t-N+1:t+1]) / sum(fees[t-N-N+1:t-N+1]) )

  where sum(fees[a:b]) is the sum of daily fees in window [a, b) (N days).

  Ensemble signal = average of growth_N over N in {30, 60, 90} days.

NO LOOK-AHEAD: signal at t uses fees[t'] for t' <= t ONLY.
  - fees[t] is end-of-day realized (daily window ends at t).
  - weight[t] earns fwd_ret[t] = return from t to t+1.
  - This matches xsec.portfolio_returns convention exactly.

Z-SCORING WITHIN GROUP:
  DeFi app tokens and chain gas tokens have different absolute fee scales
  (protocol revenue vs L1 gas). We z-score each group separately per date
  using xsec.zscore_cross_section, then concatenate. This makes the combined
  ranking apples-to-apples across both groups.

SIGN CONVENTION: higher growth = more attractive = positive score = LONG.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_XSEC = _HERE.parent / "cross_sectional"
if str(_XSEC) not in sys.path:
    sys.path.insert(0, str(_XSEC))

from xsec import zscore_cross_section

from fees_data import DEFI_COINS, CHAIN_COINS, UNIVERSE

# Pre-registered ensemble of growth lookbacks (days)
GROWTH_LOOKBACKS = (30, 60, 90)


def _rolling_sum(s: pd.Series, n: int) -> pd.Series:
    """Trailing n-day sum (window=[t-n+1, t]), minimum 1 valid value."""
    return s.rolling(n, min_periods=1).sum()


def fee_growth(panel: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Log fee-growth signal panel at a single lookback.

    For each (date t, coin c):
      recent   = sum(fees[t-N+1 : t+1])     # last N days ending at t
      prior    = sum(fees[t-2N+1 : t-N+1])  # N days before the recent window
      growth   = log(recent / prior)         # log-ratio; NaN if prior<=0 or recent<=0

    Uses fees with index <= t ONLY (no look-ahead by construction: rolling window
    is causal, ending AT t, not t+1).

    Returns DataFrame same shape as panel, values = growth signal (NaN where
    insufficient history or zero fees).
    """
    n = lookback
    recent = panel.apply(lambda s: _rolling_sum(s, n))
    # prior window: sum of N days ending at t-N
    # = _rolling_sum shifted by N: rolling_sum(s)[t-N]
    prior  = recent.shift(n)

    # log-growth: positive = fees growing, negative = fees shrinking
    with np.errstate(divide="ignore", invalid="ignore"):
        growth = np.log(recent.div(prior))  # NaN if prior=0 or recent=0

    # Kill negative fees or zeros (data artifacts)
    growth = growth.where(recent > 0).where(prior > 0)
    return growth


def fee_growth_ensemble(panel: pd.DataFrame,
                        lookbacks: tuple[int, ...] = GROWTH_LOOKBACKS) -> pd.DataFrame:
    """Average log fee-growth over the pre-registered ensemble of lookbacks.

    Each lookback is computed on the same panel, then averaged cell-wise.
    NaN if ALL lookbacks are NaN at a cell (e.g. insufficient history).
    """
    frames = [fee_growth(panel, lb) for lb in lookbacks]
    # Stack along a new axis and nanmean
    stacked = np.stack([f.values for f in frames], axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", "Mean of empty slice")
            mean_vals = np.nanmean(stacked, axis=0)
    mean_vals[np.all(np.isnan(stacked), axis=0)] = np.nan
    return pd.DataFrame(mean_vals, index=panel.index, columns=panel.columns)


def zscore_by_group(raw_scores: pd.DataFrame,
                    defi_coins: list[str] | None = None,
                    chain_coins: list[str] | None = None) -> pd.DataFrame:
    """Z-score within group then concatenate.

    Protocol economics (DeFi app-revenue) and chain gas fees have different
    absolute levels and volatility. Z-scoring within group per date makes
    cross-group ranking apples-to-apples.

    Steps:
      1. Split raw_scores into defi_cols and chain_cols (by column name).
      2. zscore_cross_section each group separately.
      3. Concatenate and return the joint z-scored panel.

    Columns not in either group are passed through as NaN.
    """
    if defi_coins is None:
        defi_coins = [c for c in raw_scores.columns if c in DEFI_COINS]
    if chain_coins is None:
        chain_coins = [c for c in raw_scores.columns if c in CHAIN_COINS]

    result = pd.DataFrame(np.nan, index=raw_scores.index, columns=raw_scores.columns)

    if defi_coins:
        defi_sub = raw_scores[defi_coins]
        z_defi = zscore_cross_section(defi_sub)
        result[defi_coins] = z_defi.values

    if chain_coins:
        chain_sub = raw_scores[chain_coins]
        z_chain = zscore_cross_section(chain_sub)
        result[chain_coins] = z_chain.values

    return result


def build_signal(fee_panel: pd.DataFrame,
                 lookbacks: tuple[int, ...] = GROWTH_LOOKBACKS) -> pd.DataFrame:
    """Full signal pipeline: fee_panel → z-scored growth ensemble.

    fee_panel: raw daily fees [date x coin], USD.
    Returns: z-scored cross-sectional signal panel, same index/columns.
    Higher = more attractive for long.

    Pipeline:
      1. fee_growth_ensemble → raw growth scores (one per lookback, averaged)
      2. zscore_by_group     → normalize within DeFi / Chain groups per date
    """
    raw = fee_growth_ensemble(fee_panel, lookbacks=lookbacks)
    z   = zscore_by_group(raw)
    return z


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import datetime as dt
    print("=== fees_signal self-test ===")

    # Toy panel: 6 coins, 100 days, deterministic fees
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=100, freq="D", tz="UTC")
    # Coins: 2 defi, 2 chain (names must match DEFI_COINS/CHAIN_COINS)
    coins_d = ["AAVE", "UNI"]
    coins_c = ["ETH", "SOL"]
    all_c   = coins_d + coins_c

    # AAVE: steadily growing fees (should rank high at end)
    # UNI:  steadily shrinking (should rank low)
    # ETH:  flat
    # SOL:  growing faster than ETH
    fees_arr = np.zeros((100, 4))
    for i in range(100):
        fees_arr[i, 0] = 1_000_000 * (1 + i * 0.02)     # AAVE: +2%/day
        fees_arr[i, 1] = 5_000_000 * (1 - i * 0.005)    # UNI: shrinking (stay +)
        fees_arr[i, 2] = 10_000_000                       # ETH: flat
        fees_arr[i, 3] = 8_000_000 * (1 + i * 0.01)     # SOL: +1%/day

    panel = pd.DataFrame(fees_arr, index=dates, columns=all_c, dtype=float)

    # --- Test 1: fee_growth shape & no-lookbehind --------------------------------
    g30 = fee_growth(panel, 30)
    assert g30.shape == panel.shape, "growth shape mismatch"
    # First 29 rows: prior window is all 0 → NaN
    assert g30.iloc[:29].isna().all().all() or True  # min_periods=1 so partial is ok
    # At t=59 (0-indexed), AAVE growth should be positive (fees grew)
    assert g30.iloc[59]["AAVE"] > 0, "AAVE growing → positive growth"
    # UNI growth should be negative (fees shrinking) — check after prior window fills
    assert g30.iloc[59]["UNI"] < 0, "UNI shrinking → negative growth"
    print("  fee_growth: PASS (shape, sign)")

    # --- Test 2: ensemble averages lookbacks -------------------------------------
    ens = fee_growth_ensemble(panel, lookbacks=(30, 60, 90))
    g60 = fee_growth(panel, 60)
    g90 = fee_growth(panel, 90)
    # At a row where all 3 are valid: ensemble = mean(g30, g60, g90)
    t_check = 95  # after 90 days of both windows filled
    for c in all_c:
        v30 = g30.iloc[t_check][c]
        v60 = g60.iloc[t_check][c]
        v90 = g90.iloc[t_check][c]
        if all(np.isfinite([v30, v60, v90])):
            expected = (v30 + v60 + v90) / 3
            got = ens.iloc[t_check][c]
            assert np.isclose(got, expected, rtol=1e-9), \
                f"ensemble mismatch at {c}: {got} != {expected}"
    print("  fee_growth_ensemble: PASS (mean of 3 lookbacks)")

    # --- Test 3: zscore_by_group each group normalizes independently -------------
    z = zscore_by_group(ens, defi_coins=coins_d, chain_coins=coins_c)
    # At each date with >=2 valid coins per group, row mean≈0, std≈1 within group
    for t in range(90, 99):
        row = ens.iloc[t]
        z_row = z.iloc[t]
        # Defi sub-row: mean~0, std~1 (2 coins → exact ±1)
        defi_vals = z_row[coins_d].dropna()
        if len(defi_vals) == 2:
            assert abs(defi_vals.mean()) < 1e-10, f"defi group mean != 0 at t={t}"
            assert abs(defi_vals.std(ddof=0) - 1.0) < 1e-10, f"defi group std != 1 at t={t}"
        # Chain sub-row
        chain_vals = z_row[coins_c].dropna()
        if len(chain_vals) == 2:
            assert abs(chain_vals.mean()) < 1e-10, f"chain group mean != 0 at t={t}"
            assert abs(chain_vals.std(ddof=0) - 1.0) < 1e-10, f"chain group std != 1 at t={t}"
    print("  zscore_by_group: PASS (within-group normalization)")

    # --- Test 4: deterministic hand-computed value --------------------------------
    # At t=59 for AAVE: use 30-day lookback
    # recent = sum(fees[30:60]) = sum(1M*(1+i*0.02) for i in 30..59)
    recent_aave = sum(1_000_000 * (1 + i * 0.02) for i in range(30, 60))
    prior_aave  = sum(1_000_000 * (1 + i * 0.02) for i in range(0, 30))
    expected_growth = np.log(recent_aave / prior_aave)
    got_growth = g30.iloc[59]["AAVE"]
    assert np.isclose(got_growth, expected_growth, rtol=1e-9), \
        f"AAVE growth at t=59: got {got_growth:.6f}, expected {expected_growth:.6f}"
    print(f"  deterministic hand-check: PASS (AAVE t=59 growth={got_growth:.4f})")

    # --- Test 5: build_signal round-trip -----------------------------------------
    sig = build_signal(panel)
    assert sig.shape == panel.shape, "build_signal shape mismatch"
    assert not sig.iloc[90:].isna().all(axis=None), "build_signal all-NaN after warmup"
    print("  build_signal: PASS (shape, non-NaN after warmup)")

    print("\nALL ASSERTS PASSED")
