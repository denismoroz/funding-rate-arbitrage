"""
generators/bootstrap.py — Stationary block bootstrap generator (T4).

Model overview
--------------
A NON-PARAMETRIC alternative to parametric.generate: historical hourly blocks of
(close, fundingRate) are resampled WITH REPLACEMENT, preserving autocorrelation
structure (within blocks), true fat tails (no distribution assumption), and
cross-coin correlations (blocks are drawn SYNCHRONOUSLY for all coins).

IMPORTANT CAVEAT (document and accept):
The 5-coin joint intersection (all of BTC, ETH, SOL, HYPE, PURR together)
spans approximately 2025-11-06 → 2026-05-12, roughly 4 500 h (~6 months).
This window is PREDOMINANTLY COLD (low/negative funding) because HYPE and PURR
only have price history from 2025-11.  Therefore:

  * Bootstrap answers the question "what if the recent regime, reshuffled, continues?"
  * It does NOT generate hot-funding episodes for the 5-coin book — that requires
    the parametric generator (T3) which can extrapolate hot regimes from calibration.
  * This is EXPECTED AND CORRECT, not a bug.  Both generators are complementary.

Fewer-coin subsets (e.g. BTC/ETH/SOL only) would give a longer and hotter window
but that is a future option — kept out of scope for T4 to stay within the contract.

Algorithm
---------
Stationary (geometric) block bootstrap, JOINT across all coins:

1.  Load real hourly series for all `coins` on their COMMON intersection of
    timestamps (inner join of fundingRate and close after hourly alignment).
    This is done exactly as calibrate_stats does it.  Result: T real rows.

2.  Compute log-returns for price:  lr[t] = log(close[t] / close[t-1])
    The first row (t=0) has no prior, so it is dropped from the log-return series;
    funding is aligned to the same truncated index.

3.  Stationary bootstrap draw:
    a. Draw a starting index  s_j  ~ Uniform{0, …, T-1}   (with wrap-around).
    b. Draw a block length    L_j  ~ Geometric(p=1/mean_block_h), clipped to [1, T].
    c. Extract indices  [s_j, s_j+1, …, s_j+L_j-1] mod T   (circular wrap).
    d. Repeat steps (a-c) appending blocks until total collected >= horizon_h.
    e. Truncate to exactly horizon_h rows.
    The SAME sequence of (s_j, L_j) pairs is applied to ALL coins simultaneously,
    so cross-coin correlations present in each block are inherited intact.

4.  Reconstruct per-coin:
    *  fundingRate: take resampled funding values directly.
    *  close: rebuild from resampled log-returns via cumulative product:
         close[0] = 100
         close[t] = 100 * exp( sum(lr[1..t]) )
       Using log-returns rather than raw price levels avoids level-discontinuities
       at block boundaries (each block joins at the running log-price level).

5.  Assemble output DataFrames with a fresh hourly DatetimeIndex from `start`,
    length exactly horizon_h.

Determinism
-----------
All randomness comes from np.random.default_rng(seed).  The draw sequence is:
  geometric block lengths → uniform start indices (both drawn in one call each,
  preallocated to max_blocks = 2 * horizon_h to avoid repeated allocation).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MEAN_BLOCK_H = 168  # 1 week in hours


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _load_real_data(
    data_dir: Path,
    coins: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load close prices and funding rates on their COMMON hourly intersection.

    Returns (price_df, funding_df):
        price_df    — DataFrame[coins], hourly close prices, common index
        funding_df  — DataFrame[coins], hourly funding rates, common index

    Both DataFrames share the same DatetimeIndex (inner join of all coins).
    """
    price_series: dict[str, pd.Series] = {}
    fund_series: dict[str, pd.Series] = {}

    for coin in coins:
        # --- funding ---
        f_path = data_dir / f"{coin}.csv"
        f_df = pd.read_csv(f_path, usecols=["time", "fundingRate"])
        f_df["time"] = pd.to_datetime(f_df["time"], format="mixed", utc=True)
        f_df = (
            f_df.sort_values("time")
            .drop_duplicates("time", keep="last")
            .set_index("time")
        )
        fund_series[coin] = f_df["fundingRate"].resample("1h").mean().dropna()

        # --- price ---
        p_path = data_dir / f"{coin}_1h.csv"
        p_df = pd.read_csv(p_path, usecols=lambda c: c in {"time", "close"})
        p_df["time"] = pd.to_datetime(p_df["time"], utc=True)
        p_df = (
            p_df.sort_values("time")
            .drop_duplicates("time", keep="last")
            .set_index("time")
        )
        price_series[coin] = p_df["close"].resample("1h").last().dropna()

    # Align all series on the common intersection
    fund_df = pd.concat(fund_series, axis=1).dropna()
    price_df = pd.concat(price_series, axis=1).dropna()

    # Common intersection of both
    common_idx = fund_df.index.intersection(price_df.index)
    fund_df = fund_df.loc[common_idx]
    price_df = price_df.loc[common_idx]

    return price_df, fund_df


# ---------------------------------------------------------------------------
# Block index generator
# ---------------------------------------------------------------------------

def _draw_block_indices(
    rng: np.random.Generator,
    T: int,
    horizon_h: int,
    mean_block_h: int,
) -> np.ndarray:
    """Draw circular block-bootstrap indices for horizon_h steps.

    Uses the stationary bootstrap (Politis & Romano 1994):
      - block lengths ~ Geometric(p=1/mean_block_h) clipped to [1, T]
      - start positions ~ Uniform{0, …, T-1}
      - wrap-around (circular) for out-of-range positions

    To avoid repeated RNG calls, pre-allocates enough draws assuming average
    block length = mean_block_h (with a 3× safety margin).

    Returns an int64 ndarray of shape (horizon_h,) with values in [0, T-1].
    """
    p = 1.0 / max(mean_block_h, 1)

    # Pre-allocate draws: expected blocks = horizon_h / mean_block_h
    # Use 3× safety margin to almost certainly have enough
    max_blocks = max(int(3 * horizon_h / mean_block_h) + 50, 100)

    # Draw all block lengths and starts at once
    lengths = rng.geometric(p, size=max_blocks).clip(1, T)
    starts = rng.integers(0, T, size=max_blocks)

    indices = np.empty(horizon_h, dtype=np.int64)
    filled = 0
    block_i = 0

    while filled < horizon_h:
        if block_i >= max_blocks:
            # Safety: draw more blocks (rare edge case)
            extra = max(int(1.5 * (horizon_h - filled) / mean_block_h) + 20, 20)
            lengths = np.concatenate([lengths, rng.geometric(p, size=extra).clip(1, T)])
            starts = np.concatenate([starts, rng.integers(0, T, size=extra)])
            max_blocks += extra

        s = int(starts[block_i])
        L = int(lengths[block_i])
        block_i += 1

        need = horizon_h - filled
        take = min(L, need)

        # Circular wrap
        positions = np.arange(s, s + take, dtype=np.int64) % T
        indices[filled: filled + take] = positions
        filled += take

    return indices


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(
    data_dir: str | Path,
    horizon_h: int,
    seed: int,
    coins: list[str],
    start: str = "2024-01-01",
    mean_block_h: int = _DEFAULT_MEAN_BLOCK_H,
) -> dict[str, pd.DataFrame]:
    """Generate bootstrap-resampled synthetic dfs dict.

    Arguments:
        data_dir:      Path to research/data/ directory (same convention as
                       calibrate_stats: contains {coin}.csv / {coin}_1h.csv).
        horizon_h:     Target horizon in hours.
        seed:          Integer random seed for full determinism.
        coins:         List of coin symbols to generate.
        start:         ISO date string for first DatetimeIndex timestamp
                       (timezone-naive, hourly frequency, like parametric.py).
        mean_block_h:  Mean block length in hours for the stationary bootstrap.
                       Default 168 (1 week) — preserves multi-day autocorrelation
                       structure of funding rates (empirical AR(1) ~0.88).

    Returns:
        dict mapping coin → pd.DataFrame with columns ['close', 'fundingRate']
        and a timezone-naive hourly DatetimeIndex of length horizon_h starting
        at `start`.  close starts at 100.0 by construction.

    Caveat:
        The 5-coin intersection is ~4 500 h (2025-11 → 2026-05), predominantly
        cold regime.  See module docstring for full discussion.

    Raises:
        ValueError: if coins list is empty or any coin data is missing.
    """
    if not coins:
        raise ValueError("coins list must not be empty")

    data_dir = Path(data_dir)
    rng = np.random.default_rng(seed)

    # ── Load real data on common intersection ────────────────────────────────
    price_df, fund_df = _load_real_data(data_dir, coins)

    T = len(price_df)
    if T < 2:
        raise ValueError(
            f"Common intersection has only {T} rows — insufficient for bootstrap. "
            "Check data availability for all requested coins."
        )

    # Validate coins are present
    missing_p = [c for c in coins if c not in price_df.columns]
    missing_f = [c for c in coins if c not in fund_df.columns]
    if missing_p or missing_f:
        raise ValueError(
            f"Missing price data: {missing_p}; missing funding data: {missing_f}"
        )

    # ── Compute log-returns for price ─────────────────────────────────────────
    # Drop first row (no prior for log-return) → aligned price and funding both have T-1 rows
    price_vals = price_df[coins].values  # shape (T, n_coins)
    fund_vals = fund_df[coins].values    # shape (T, n_coins)

    log_ret_vals = np.log(price_vals[1:] / price_vals[:-1])  # shape (T-1, n_coins)
    fund_aligned = fund_vals[1:]                               # shape (T-1, n_coins)
    T_eff = T - 1  # effective sample size for resampling

    # ── Draw synchronous block indices (same for ALL coins) ───────────────────
    indices = _draw_block_indices(rng, T_eff, horizon_h, mean_block_h)

    # ── Resample ──────────────────────────────────────────────────────────────
    sampled_lr = log_ret_vals[indices]   # shape (horizon_h, n_coins)
    sampled_fr = fund_aligned[indices]   # shape (horizon_h, n_coins)

    # ── Reconstruct price from log-returns ────────────────────────────────────
    # close[0] = 100;  close[t] = 100 * exp(cumsum of log-returns up to t)
    # Prepend 0 so that exp(cumsum) starts at 1 at t=0
    cum_log_ret = np.concatenate(
        [np.zeros((1, len(coins))), np.cumsum(sampled_lr, axis=0)],
        axis=0,
    )  # shape (horizon_h + 1, n_coins)
    price_resampled = 100.0 * np.exp(cum_log_ret[:-1])  # shape (horizon_h, n_coins)

    # ── Assemble DataFrames ───────────────────────────────────────────────────
    idx = pd.date_range(start=start, periods=horizon_h, freq="h")

    result: dict[str, pd.DataFrame] = {}
    for i, coin in enumerate(coins):
        result[coin] = pd.DataFrame(
            {
                "close": price_resampled[:, i],
                "fundingRate": sampled_fr[:, i],
            },
            index=idx,
        )

    return result


# ---------------------------------------------------------------------------
# Helpers for testing / round-trip validation
# ---------------------------------------------------------------------------

def compute_real_intersection_stats(
    data_dir: str | Path,
    coins: list[str],
) -> dict[str, dict]:
    """Compute statistics from the real intersection window actually used by bootstrap.

    Because the bootstrap resamples exclusively from the common intersection of all
    `coins`, the right reference for marginal-preservation tests is NOT the full
    calibration history (which may span 2023–2026 for BTC/ETH/SOL) but ONLY the
    intersection window.  This function extracts those statistics.

    Returns:
        dict mapping coin → {
            "negative_hours_share":     float,
            "funding_mean_annual_pct":  float,
            "funding_std_h":            float,
            "funding_ar1_phi":          float,
        }
    """
    data_dir = Path(data_dir)
    price_df, fund_df = _load_real_data(data_dir, coins)

    result: dict[str, dict] = {}
    for coin in coins:
        fr = fund_df[coin]
        x = fr.values
        n = len(x)

        neg_share = float((x < 0).mean())
        fund_mean_h = float(x.mean())
        fund_std_h = float(x.std(ddof=1))

        # AR(1) via OLS
        if n >= 2:
            y = x[1:]
            xp = x[:-1]
            xp_dm = xp - xp.mean()
            y_dm = y - y.mean()
            denom = float(np.dot(xp_dm, xp_dm))
            phi = float(np.clip(np.dot(xp_dm, y_dm) / denom, -0.9999, 0.9999)) if denom > 0 else 0.0
        else:
            phi = 0.0

        result[coin] = {
            "negative_hours_share": neg_share,
            "funding_mean_annual_pct": fund_mean_h * 8760 * 100,
            "funding_std_h": fund_std_h,
            "funding_ar1_phi": phi,
        }

    return result


def compute_real_intersection_acf(
    data_dir: str | Path,
    coins: list[str],
    max_lag: int = 24,
) -> dict[str, np.ndarray]:
    """Compute ACF of funding rates on the real intersection window, lags 1..max_lag.

    Returns dict mapping coin → ndarray of shape (max_lag,) with ACF[lag-1] = ACF(lag).
    """
    data_dir = Path(data_dir)
    _price_df, fund_df = _load_real_data(data_dir, coins)

    result: dict[str, np.ndarray] = {}
    for coin in coins:
        x = fund_df[coin].values.astype(float)
        x_dm = x - x.mean()
        var = float(np.dot(x_dm, x_dm))
        acf = np.zeros(max_lag, dtype=float)
        for lag in range(1, max_lag + 1):
            acf[lag - 1] = float(np.dot(x_dm[:-lag], x_dm[lag:]) / var)
        result[coin] = acf

    return result


def compute_real_intersection_cross_corr(
    data_dir: str | Path,
    coins: list[str],
) -> np.ndarray:
    """Compute cross-correlation matrix of funding on the real intersection window.

    Returns ndarray of shape (n_coins, n_coins) with Pearson correlations.
    Coin order matches the `coins` list.
    """
    data_dir = Path(data_dir)
    _price_df, fund_df = _load_real_data(data_dir, coins)

    fund_sub = fund_df[coins]
    return fund_sub.corr().values
