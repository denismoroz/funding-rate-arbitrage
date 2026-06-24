"""
Five signal/structure variants for XSMOM signal-improvement validation.

Pre-registered in PLAN.md. All functions return score panels pd.DataFrame[date x coin]
where HIGHER = more attractive to LONG. Feeds xsec.rank_to_weights.

NO look-ahead: every score at row t uses only data with index <= t.
fwd_ret is NEVER read here. Signals built on the full panel once; CPCV masks rows.

Arms:
  R — risk-adjusted momentum (mean/std, or t-stat mean/std*sqrt(n))
  G — skip-recent gap momentum [t-lb, t-gap]
  K — rank-based (percentile) signal instead of z-score
  T — TS×XS gate: keep XS long only if coin's own trend is up, XS short only if down
  B — breadth sweep: vary top/bottom fraction frac ∈ {1/5, 1/3, 1/2}
      (breadth changes leg selection — produce final weight panels not score panels)
"""

from __future__ import annotations

import warnings
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── Import baseline code ────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_crypto_dir = str(_HERE.parent / "cross_sectional" / "crypto")
_xsec_dir = str(_HERE.parent / "cross_sectional")
for _d in [_crypto_dir, _xsec_dir]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

import signals as _signals      # signals.momentum, zscore_cross_section
import xsec as _xsec            # rank_to_weights

# ── Constants (match live XSMOM) ───────────────────────────────────────────────
LOOKBACKS = (14, 21, 30, 45, 60)
MAX_LOOKBACK = max(LOOKBACKS)          # 60


# ═══════════════════════════════════════════════════════════════════════════════
# ARM R — Risk-adjusted momentum (Sharpe-ratio-of-returns signal)
# ═══════════════════════════════════════════════════════════════════════════════

def _risk_adj_mom_one(price: pd.DataFrame, lb: int, tstat: bool = False) -> pd.DataFrame:
    """score = mean(daily_ret over lb days) / std(daily_ret over lb days) [× sqrt(lb) if tstat].

    No look-ahead: rolling window ending at t uses daily returns only up to t.
    daily_ret[t] = price[t]/price[t-1]-1, all known at t.
    """
    daily_ret = price / price.shift(1) - 1.0
    # rolling mean and std over lb days ending at t (min_periods=lb → NaN until full window)
    roll_mean = daily_ret.rolling(lb, min_periods=lb).mean()
    roll_std  = daily_ret.rolling(lb, min_periods=lb).std(ddof=0)
    score = roll_mean / roll_std.replace(0.0, np.nan)
    if tstat:
        score = score * np.sqrt(lb)
    return score


def arm_R_sharpe(panel: dict, lookbacks: tuple = LOOKBACKS) -> pd.DataFrame:
    """Arm R — risk-adjusted momentum ensemble (Sharpe of daily returns).

    score[t,c] = mean_lb( zscore_xs( mean(ret_lb)/std(ret_lb) ) ) over lookbacks.
    Same z-score + ensemble structure as momentum_ensemble; only the per-coin
    score changes from total return to Sharpe-of-daily-returns.
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    legs = [_signals.zscore_cross_section(_risk_adj_mom_one(price, lb, tstat=False))
            for lb in lookbacks]
    return _ensemble_mean(legs)


def arm_R_tstat(panel: dict, lookbacks: tuple = LOOKBACKS) -> pd.DataFrame:
    """Arm R variant 2 — t-stat momentum ensemble (mean/std × sqrt(n)).

    score[t,c] = mean_lb( zscore_xs( mean(ret_lb)/std(ret_lb)*sqrt(lb) ) ).
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    legs = [_signals.zscore_cross_section(_risk_adj_mom_one(price, lb, tstat=True))
            for lb in lookbacks]
    return _ensemble_mean(legs)


# ═══════════════════════════════════════════════════════════════════════════════
# ARM G — Skip-recent gap momentum [t-lb, t-gap]
# ═══════════════════════════════════════════════════════════════════════════════

def _gap_mom_one(price: pd.DataFrame, lb: int, gap: int) -> pd.DataFrame:
    """Raw momentum over [t-lb, t-gap]: price[t-gap] / price[t-lb] - 1.

    gap=0 degenerates to price[t]/price[t-lb]-1 = standard momentum.
    No look-ahead: both lags reference past prices.
    """
    return price.shift(gap) / price.shift(lb) - 1.0


def arm_G(panel: dict, gap: int, lookbacks: tuple = LOOKBACKS) -> pd.DataFrame:
    """Arm G — skip-recent gap momentum ensemble.

    score[t,c] = mean_lb( zscore_xs( price[t-gap]/price[t-lb]-1 ) ) over lookbacks.
    gap ∈ {3,5,7}; gap=0 reproduces baseline (degenerate check).
    """
    if gap >= min(lookbacks):
        raise ValueError(f"gap={gap} must be < min(lookbacks)={min(lookbacks)}")
    price = panel["price"] if isinstance(panel, dict) else panel
    legs = [_signals.zscore_cross_section(_gap_mom_one(price, lb, gap))
            for lb in lookbacks]
    return _ensemble_mean(legs)


# ═══════════════════════════════════════════════════════════════════════════════
# ARM K — Rank-based (percentile) cross-sectional signal
# ═══════════════════════════════════════════════════════════════════════════════

def _percentile_rank_cross_section(scores: pd.DataFrame) -> pd.DataFrame:
    """Replace z-score with cross-sectional percentile rank, demeaned to zero.

    rank[t,c] = coin's rank / (n_valid - 1)  in [0, 1], then subtract 0.5 to
    centre at 0. NaN coins excluded from ranking; their cells stay NaN.
    Degenerate rows (<2 valid) → NaN (no useful ordering).
    Robust to outliers: a coin with 10× momentum gets rank 1.0, not a huge z.
    """
    result = scores.copy() * np.nan
    for dt, row in scores.iterrows():
        valid = row.dropna()
        n = len(valid)
        if n < 2:
            continue
        # rank from 0 (lowest) to n-1 (highest), then normalize to [0,1]
        ranks = valid.rank(method="average") - 1   # 0-based
        normed = ranks / (n - 1) - 0.5             # centre at 0
        result.loc[dt, valid.index] = normed
    return result


def arm_K(panel: dict, lookbacks: tuple = LOOKBACKS) -> pd.DataFrame:
    """Arm K — percentile-rank ensemble instead of z-score ensemble.

    score[t,c] = mean_lb( percentile_rank_xs( momentum(lb) ) ) over lookbacks.
    Same legs as baseline but percentile-rank replaces z-score at the XS step.
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    legs = [_percentile_rank_cross_section(_signals.momentum(panel, lb))
            for lb in lookbacks]
    return _ensemble_mean(legs)


# ═══════════════════════════════════════════════════════════════════════════════
# ARM T — TS×XS gate: keep XS leg only if coin's own trend aligns
# ═══════════════════════════════════════════════════════════════════════════════

def arm_T_weights(panel: dict, trend_lb: int,
                  lookbacks: tuple = LOOKBACKS,
                  tercile_frac: float = 1 / 3) -> pd.DataFrame:
    """Arm T — TS×XS gated weights.

    Start with baseline XS scores (momentum_ensemble), compute baseline weights
    (rank_to_weights). Then for each day and position:
      - If coin is XS-long (+) and its trend is DOWN (ret over trend_lb ≤ 0): → 0 (cash)
      - If coin is XS-short (-) and its trend is UP (ret over trend_lb ≥ 0): → 0 (cash)
      - Otherwise: keep weight.
    After gating, renormalize each surviving leg to ±1. If EITHER leg is fully
    wiped out (no survivors), the whole day goes flat (Σw=0, no trade). This
    preserves the dollar-neutral invariant: book is always net-zero or empty.

    trend = price[t] / price[t-trend_lb] - 1  (causal, no look-ahead).
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    # Baseline XS scores and weights
    scores = _signals.momentum_ensemble(panel, lookbacks=lookbacks)
    w_base = _xsec.rank_to_weights(scores, tercile_frac=tercile_frac)

    # Trend signal: price[t]/price[t-trend_lb]-1 > 0 means up
    trend = price / price.shift(trend_lb) - 1.0

    w_gated = w_base.copy()
    for dt in w_base.index:
        row = w_base.loc[dt]
        tr  = trend.loc[dt] if dt in trend.index else pd.Series(dtype=float)
        # identify longs / shorts in the baseline book
        longs  = row[row > 0].index
        shorts = row[row < 0].index

        # gate longs: remove if trend ≤ 0 (down or flat)
        keep_longs = [c for c in longs
                      if c in tr.index and not np.isnan(tr[c]) and tr[c] > 0]
        # gate shorts: remove if trend ≥ 0 (up or flat)
        keep_shorts = [c for c in shorts
                       if c in tr.index and not np.isnan(tr[c]) and tr[c] < 0]

        # Dollar-neutral: BOTH sides must survive; if either is wiped, go flat.
        # This preserves the dollar-neutral invariant at all times.
        new_row = pd.Series(0.0, index=row.index)
        if keep_longs and keep_shorts:
            new_row[keep_longs]  = 1.0 / len(keep_longs)
            new_row[keep_shorts] = -1.0 / len(keep_shorts)
        # else: entirely flat that day (no trade) — Σw = 0 ✓
        w_gated.loc[dt] = new_row

    return w_gated


# ═══════════════════════════════════════════════════════════════════════════════
# ARM B — Breadth sweep (vary top/bottom fraction)
# ═══════════════════════════════════════════════════════════════════════════════

def arm_B_weights(panel: dict, frac: float,
                  lookbacks: tuple = LOOKBACKS) -> pd.DataFrame:
    """Arm B — breadth sweep: top/bottom frac of valid coins.

    frac ∈ {1/5, 1/3, 1/2}. frac=1/3 reproduces baseline (degenerate check).
    Uses same baseline momentum_ensemble scores, only rank_to_weights(frac) changes.
    """
    scores = _signals.momentum_ensemble(panel, lookbacks=lookbacks)
    return _xsec.rank_to_weights(scores, tercile_frac=frac)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helper
# ═══════════════════════════════════════════════════════════════════════════════

def _ensemble_mean(legs: list[pd.DataFrame]) -> pd.DataFrame:
    """Equal-weight mean of z/rank legs; NaN if ANY leg missing (same logic as momentum_ensemble)."""
    if not legs:
        raise ValueError("_ensemble_mean: need at least one leg")
    idx, cols = legs[0].index, legs[0].columns
    legs = [leg.reindex(index=idx, columns=cols) for leg in legs]
    arr = np.stack([leg.values for leg in legs], axis=0)     # (n_lb, T, C)
    all_present = ~np.isnan(arr).any(axis=0)                 # all lookbacks defined
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean = np.nanmean(arr, axis=0)
    mean = np.where(all_present, mean, np.nan)
    return pd.DataFrame(mean, index=idx, columns=cols)
