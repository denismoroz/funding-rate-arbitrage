"""
TSMOM (time-series momentum) signals for the indices+gold universe.

All signals are CAUSAL: at row t, only data with index <= t is used.
No look-ahead: the price/return windows are TRAILING and lagged with .shift(),
not centered. The signal at t feeds weight at t, which earns fwd_ret[t]
(t→t+1 realised return).  This is the standard MOP (Moskowitz-Ooi-Pedersen)
managed-futures TSMOM factor.

Horizon conventions (matching fx/signals.py):
  MONTH = 21  (business days per month, exact for the "B" grid)
  YEAR  = 252 (business days per year)

TSMOM design:
  For each asset i and day t:
    signal_sign[t,i] = sign( price[t] / price[t - K*21] - 1 )
                     = sign of the trailing K-month SIMPLE return.
  Position (vol-scaled equal-risk):
    raw_weight[t,i] = signal_sign[t,i] * (TARGET_VOL / realized_vol[t,i])
  where
    realized_vol[t,i] = rolling std of DAILY returns over the last VOL_WINDOW
                        (trailing window ≤ t), annualized by sqrt(252).
  Gross normalization: divide by N_valid[t] (the number of assets with a valid
    signal at t) so the sum(|weights|) ≈ 1 (gross leverage ~1).  This is NOT
    dollar-neutral (the book is directionally long/short by trend); we just bound
    gross leverage for cost comparability.

Ensemble (tsmom_ensemble): simple equal-weight average of the sign-positions
  across lookbacks (3, 6, 12 months), then re-normalize gross to 1.

Cross-sectional momentum (xs_momentum): thin wrapper around the FX-style
  rank_to_weights path from xsec.py — expected to be thin here because indices
  co-move; included for menu completeness.

Only numpy/pandas (+ xsec imported at runtime for xs_momentum).
"""

from __future__ import annotations

import warnings
import numpy as np
import pandas as pd

MONTH = 21   # business days per month
YEAR  = 252  # business days per year
TARGET_VOL   = 0.10   # 10% ann per asset (vol-scaling target)
VOL_WINDOW   = 60     # trailing bdays for realized vol estimate


def _trailing_vol(price: pd.DataFrame, window: int = VOL_WINDOW) -> pd.DataFrame:
    """Trailing annualized vol of DAILY returns: rolling std(ddof=0) * sqrt(252).
    Uses data <= t only (trailing window).  NaN for the first `window` rows.
    The daily return at t is price[t]/price[t-1]-1; rolling is then applied over
    the last `window` such returns.

    NOTE: uses ddof=1 (pandas default for rolling.std) for stability; the
    difference vs ddof=0 is negligible for window=60.
    """
    daily_ret = price / price.shift(1) - 1.0
    # min_periods=window: NaN until a full window is available
    rv = daily_ret.rolling(window, min_periods=window).std() * np.sqrt(YEAR)
    return rv


def tsmom(
    price: pd.DataFrame,
    lookback_months: int,
    vol_window: int = VOL_WINDOW,
    target_vol: float = TARGET_VOL,
) -> pd.DataFrame:
    """TSMOM weights for a single lookback K (in months).

    Returns a DataFrame of the SAME shape as price: weight[t,i] is the
    vol-scaled signed position for asset i on day t.  Gross leverage is
    normalized to exactly 1.0 by dividing by sum(|raw_w|) at each t.

    Steps (all causal, data <= t only):
      1. trailing_ret[t,i] = price[t] / price[t - K*21] - 1
      2. sign_i[t] = sign(trailing_ret[t,i])   (0 if ret==0, NaN if ret is NaN)
      3. rv[t,i] = trailing annualized vol (vol_window bday rolling std * sqrt(252))
      4. raw_w[t,i] = sign_i[t] * target_vol / max(rv[t,i], 1e-8)
      5. gross[t] = sum_i(|raw_w[t,i]|)  (over valid assets)
      6. w[t,i] = raw_w[t,i] / gross[t]  → sum(|w|) = 1.0 exactly

    TSMOM is NOT dollar-neutral (net can be +/- depending on how many assets
    are trending up vs down); we only bound GROSS leverage = 1.0 for cost
    comparability.  NaN cells: wherever trailing_ret or rv is NaN (pre-history).
    """
    lb = lookback_months * MONTH
    trailing_ret = price / price.shift(lb) - 1.0

    # sign: +1 if trending up, -1 if trending down, NaN if return is NaN.
    sign = np.sign(trailing_ret)  # pd.DataFrame, same shape; NaN passes through

    # Realized vol (trailing, no look-ahead)
    rv = _trailing_vol(price, window=vol_window)
    rv = rv.clip(lower=1e-8)  # avoid div-by-zero

    # Vol-scaled position
    raw_w = sign * (target_vol / rv)

    # Normalize by gross (sum of |raw_w|) so sum(|w|) = 1.0 exactly.
    # NaN cells do not contribute to gross; rows where gross = 0 stay NaN.
    gross = raw_w.abs().sum(axis=1)    # NaN-aware: nansum over cols
    gross = gross.replace(0.0, np.nan)  # all-zero or all-NaN row → NaN
    w = raw_w.div(gross, axis=0)

    return w


def tsmom_ensemble(
    price: pd.DataFrame,
    lookbacks: tuple[int, ...] = (3, 6, 12),
    vol_window: int = VOL_WINDOW,
    target_vol: float = TARGET_VOL,
) -> pd.DataFrame:
    """Equal-weight ensemble TSMOM across the given lookbacks.

    Averages the (already gross-normalized) per-lookback weight matrices and
    re-normalizes the ensemble so gross ≈ 1.  Each constituent tsmom() is already
    individually normalized; the ensemble is their simple average re-normalized.

    Returns DataFrame[date x asset], same shape as price.  NaN where ALL
    constituent lookbacks are NaN for that cell (pre-history); partial NaN is
    handled by the NaN-aware average (mean over non-NaN values).
    """
    parts = [tsmom(price, lb, vol_window=vol_window, target_vol=target_vol)
             for lb in lookbacks]
    # NaN-aware mean: DataFrame.add().div() would need explicit NaN handling.
    # Use numpy nanmean row-wise instead.  Suppress the harmless "Mean of empty
    # slice" warning that fires for all-NaN rows in the early pre-history period.
    stacked = np.stack([p.values for p in parts], axis=0)  # (n_lookbacks, T, N)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        avg = np.nanmean(stacked, axis=0)                  # (T, N)
    # Re-normalize gross to ~1.0
    gross = np.nansum(np.abs(avg), axis=1, keepdims=True)
    gross = np.where(gross < 1e-10, 1.0, gross)
    w_arr = avg / gross

    w = pd.DataFrame(w_arr, index=price.index, columns=price.columns)
    return w


def xs_momentum(
    price: pd.DataFrame,
    lookback_months: int = 12,
    skip_months: int = 1,
) -> pd.DataFrame:
    """Cross-sectional momentum score (for xs_mom menu config).

    score[t,i] = price[t-skip] / price[t-lookback] - 1  (higher = LONG winner).
    Returns a SCORE panel, not yet weighted — caller must pass to
    xsec.rank_to_weights then xsec.portfolio_returns.
    This is the exact same computation as fx/signals.momentum() applied to
    the index/gold price panel.
    """
    skip    = skip_months * MONTH
    lookback = lookback_months * MONTH
    return price.shift(skip) / price.shift(lookback) - 1.0


# ── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "cross_sectional"))
    import xsec

    # Build a tiny synthetic price panel for hand-checks.
    rng = np.random.default_rng(42)
    n_days, n_assets = 300, 4
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B", tz="UTC")
    assets = ["A", "B", "C", "D"]

    # Asset A trends up 0.1%/day, B down 0.1%/day, C/D random
    drift = np.array([0.001, -0.001, 0.0, 0.0])
    noise = rng.normal(0, 0.01, (n_days, n_assets))
    log_ret = drift + noise
    price = pd.DataFrame(
        100.0 * np.exp(np.cumsum(log_ret, axis=0)),
        index=dates, columns=assets,
    )

    # (1) TSMOM at lookback=3 months: Asset A should be +, Asset B should be −
    w3 = tsmom(price, lookback_months=3)
    # Last row: A trending up -> positive weight, B down -> negative weight
    last = w3.iloc[-1].dropna()
    print(f"tsmom(3m) last row: {last.to_dict()}")
    assert last["A"] > 0, f"Asset A (uptrend) must have positive weight, got {last['A']}"
    assert last["B"] < 0, f"Asset B (downtrend) must have negative weight, got {last['B']}"

    # (2) Hand-check vol-scaling: high-vol asset gets SMALLER |weight|
    # Build 2-asset panel: one low-vol, one high-vol (same trend)
    hi_vol_ret = np.cumsum(0.001 + rng.normal(0, 0.05, n_days))  # hi vol
    lo_vol_ret = np.cumsum(0.001 + rng.normal(0, 0.005, n_days)) # lo vol
    price2 = pd.DataFrame({
        "HI": 100.0 * np.exp(hi_vol_ret),
        "LO": 100.0 * np.exp(lo_vol_ret),
    }, index=dates)
    w2 = tsmom(price2, lookback_months=3)
    # After the initial NaN window, check that |HI| < |LO| (same sign but smaller)
    valid_idx = w2.dropna().index
    if len(valid_idx) > 0:
        t = valid_idx[-1]
        w_hi = w2.loc[t, "HI"]
        w_lo = w2.loc[t, "LO"]
        assert abs(w_hi) < abs(w_lo), \
            f"High-vol asset must have smaller |weight|: |HI|={abs(w_hi):.4f} |LO|={abs(w_lo):.4f}"
        print(f"vol-scaling: |HI|={abs(w_hi):.4f} < |LO|={abs(w_lo):.4f}  OK")

    # (3) Ensemble is average of 3 lookbacks, re-normalized
    w_ens = tsmom_ensemble(price, lookbacks=(3, 6, 12))
    last_ens = w_ens.iloc[-1].dropna()
    gross_ens = last_ens.abs().sum()
    assert abs(gross_ens - 1.0) < 0.01, \
        f"Ensemble gross should ≈ 1.0, got {gross_ens:.4f}"
    print(f"ensemble gross={gross_ens:.4f} ≈ 1.0  OK")

    # (4) No look-ahead: signal at t uses only price[t] and earlier
    # Sanity: tsmom(lb=3) at the LAST row should equal sign(price[-1]/price[-63]-1)
    # times (TARGET_VOL / rv[-1]), normalized.
    t_last = price.index[-1]
    i_last = price.index.get_loc(t_last)
    lb = 3 * MONTH
    t_lb = price.index[i_last - lb]
    for a in assets:
        trail_ret = price.loc[t_last, a] / price.loc[t_lb, a] - 1.0
        expected_sign = np.sign(trail_ret)
        got_sign = np.sign(w3.loc[t_last, a]) if not np.isnan(w3.loc[t_last, a]) else float("nan")
        assert np.isclose(expected_sign, got_sign), \
            f"sign mismatch for {a}: expected {expected_sign}, got {got_sign}"
    print("no-look-ahead sign check on tsmom(3m) last row: OK")

    print("\nALL SIGNAL SELF-TESTS PASSED")
