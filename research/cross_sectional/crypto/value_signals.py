"""
Cross-sectional VALUE / contrarian factor signals for the crypto long-short book.

All signals return a DataFrame[date x coin] of cross-sectional z-scores where
HIGHER = more attractive to LONG = "cheaper / more depressed in valuation terms".

Seam-safety guarantee (see individual function docstrings):
  - Every computation at row t uses ONLY data with index <= t.
  - Rolling/expanding windows preserve this: shift(n) gives values from n steps ago
    (no look-ahead); rolling(window).max() over the LEADING direction is NOT used
    (we never use .rolling(window).max() that looks forward).
  - fwd_ret is NEVER read here.
  - The binding purge for the CPCV harness is >= the LONGEST trailing window used
    by any signal called from this module. For the windows implemented here:
      drawdown_from_high(expanding=True) -> no fixed purge requirement (expands from
        the start, but signal is undefined for the first 2 rows)
      drawdown_from_high(window=90)      -> purge >= 90d
      drawdown_from_high(window=180)     -> purge >= 180d
      dist_from_ma(ma_window=100)        -> purge >= 100d
      dist_from_ma(ma_window=200)        -> purge >= 200d
      long_term_reversal(lookback=120)   -> purge >= 120d
      long_term_reversal(lookback=180)   -> purge >= 180d
    In run_value.py we use purge=200 (the longest window), which is safe for all.

Sign convention (all three signal families):
  "cheap" / "depressed" coin -> HIGH positive z-score -> goes to LONG leg.
  "expensive" / "elevated" coin -> LOW (negative) z-score -> goes to SHORT leg.

Only numpy/pandas.
"""

import warnings

import numpy as np
import pandas as pd


# ── Helper ─────────────────────────────────────────────────────────────────────

def _zscore_cs(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score per row (date): (x - row_mean) / row_std (ddof=0).

    Rows with <2 valid coins or zero spread → NaN. Mirrors signals.zscore_cross_section.
    """
    mean = df.mean(axis=1)
    std = df.std(axis=1, ddof=0)
    return df.sub(mean, axis=0).div(std.replace(0.0, np.nan), axis=0)


# ── Signal 1: drawdown from trailing (or all-time) high ───────────────────────

def drawdown_from_high(panel, window: int | None = None) -> pd.DataFrame:
    """Drawdown-from-peak factor: how far has each coin fallen from its recent high?

    Raw value = price[t] / trailing_max(price, window) - 1  (<= 0 always).
    A deeply-drawn-down coin is "cheap" (depressed from peak) → should get the
    HIGHEST long score. Hence:

        score = -zscore_cross_section(raw_drawdown)
              = zscore_cross_section(-raw_drawdown)   # equivalent

    Sign hand-check: raw_drawdown is negative for a fallen coin.  raw_drawdown
    is LESS negative (closer to 0) for a coin near its high. Taking the
    zscore of the raw (negative) values means the coin with the MOST NEGATIVE
    drawdown gets the LOWEST zscore. Negating flips that: most-negative raw ->
    largest positive score -> LONG. That is the correct contrarian direction.

    Parameters
    ----------
    panel : dict with key "price" (DataFrame[date x coin]), or a DataFrame directly.
    window : rolling look-back in calendar days for the trailing max.
        None (default) → EXPANDING max from the start of each coin's listed history
        (approximates all-time-high; seam-safe: expanding only uses data <= t).
    """
    price = panel["price"] if isinstance(panel, dict) else panel

    if window is None:
        # Expanding (all-time) high: expanding().max() is seam-safe —
        # at each t it uses only price data up to and including t.
        trailing_max = price.expanding(min_periods=2).max()
    else:
        # Rolling window: rolling(window, min_periods=window).max() is seam-safe
        # because all window observations end AT t (no look-ahead).
        trailing_max = price.rolling(window, min_periods=window).max()

    raw_dd = price / trailing_max - 1.0   # <= 0; NaN where trailing_max is NaN

    # Negate before z-scoring so deeply-drawn-down (large negative raw) -> high score.
    score = _zscore_cs(-raw_dd)           # = -zscore_cs(raw_dd); cheaper = more positive
    return score


# ── Signal 2: distance from moving average ────────────────────────────────────

def dist_from_ma(panel, ma_window: int = 100) -> pd.DataFrame:
    """Distance-from-MA factor: how far is price below its rolling mean?

    Raw value = price[t] / rolling_mean(price, ma_window) - 1.
    Below MA (negative) = cheap/depressed = high long score.
    Above MA (positive) = expensive = low (short) score.

    Hence score = -zscore_cross_section(raw_distance).

    Seam-safe: rolling(ma_window).mean() at t uses only price[t-ma_window+1 .. t]
    (all data <= t). The first ma_window-1 rows of each coin are NaN.

    Sign hand-check: a coin trading 20% below its 100-day MA has raw = -0.20.
    A coin trading 15% ABOVE its MA has raw = +0.15. z-scoring the raw values
    puts the -0.20 coin at a lower z than the +0.15 coin. Negating flips it:
    -0.20 -> high positive score (LONG), +0.15 -> negative score (SHORT). Correct.
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    ma = price.rolling(ma_window, min_periods=ma_window).mean()
    raw_dist = price / ma - 1.0           # negative = below MA = cheap
    score = _zscore_cs(-raw_dist)         # cheaper (below MA) -> higher score
    return score


# ── Signal 3: long-term reversal ──────────────────────────────────────────────

def long_term_reversal(panel, lookback: int = 120) -> pd.DataFrame:
    """Long-term reversal factor: past LOSERS (over 3-6 months) get long score.

    Distinct from sweep.py's short reversal (lb=3..14d, which was stably negative
    across the sweep). This is the CLASSIC contrarian horizon (120-180d), where
    long-horizon underperformers attract capital back (value-like reversion).

    Raw value = price[t] / price[t - lookback] - 1  (same as momentum at lookback).
    Past LOSER (low return) = value-cheap = high long score. Hence:

        score = -zscore_cross_section(raw_return)

    Seam-safe: price[t - lookback] = price.shift(lookback)[t], which uses only
    price data at row t - lookback (no look-ahead). First `lookback` rows are NaN.

    Sign hand-check: a coin down 40% over the past 120 days has raw = -0.40.
    A coin up 80% has raw = +0.80. z-scoring raw: the -0.40 coin gets a low z.
    Negating: the -0.40 coin gets a HIGH score -> LONG. The +0.80 coin -> SHORT.
    Contrarian / value direction confirmed.

    NOTE: at lookback=120 this is the -momentum(120) signal. Correlation with
    momentum_ensemble (lookbacks 14-60) will be negative but not -1 (different
    horizons). The key question is whether the 120-180d horizon captures something
    distinct from 14-60d momentum.
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    raw_ret = price / price.shift(lookback) - 1.0
    score = _zscore_cs(-raw_ret)          # past losers -> high score (LONG)
    return score


# ── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/research/cross_sectional/crypto")
    import cryptodata

    P = cryptodata.load_panel()
    price = P["price"]
    print(f"\n=== VALUE SIGNALS SELF-TEST ===")
    print(f"Panel: {price.shape[0]} days x {price.shape[1]} coins  "
          f"({price.index.min().date()} -> {price.index.max().date()})")

    # ── Pick a reference coin and a well-defined recent date ──────────────────
    c = "BTC"
    c_listed = price[c].dropna().index

    # ── Signal 1a: drawdown_from_high(window=90) ──────────────────────────────
    dd90 = drawdown_from_high(P, window=90)

    # Hand-check one cell: manual rolling max over last 90 rows
    t = c_listed[-5]
    i = price.index.get_loc(t)
    window = 90
    trailing_max_manual = price[c].iloc[max(0, i - window + 1): i + 1].max()
    raw_dd_manual = price.loc[t, c] / trailing_max_manual - 1.0

    # Compare against what the full z-score implies (we can't directly compare z,
    # but we can check the raw dd before zscore):
    trailing_max_series = price.rolling(90, min_periods=90).max()
    raw_dd_full = (price / trailing_max_series - 1.0)
    assert np.isclose(raw_dd_full.loc[t, c], raw_dd_manual), \
        f"drawdown raw mismatch: {raw_dd_full.loc[t, c]:.6f} != {raw_dd_manual:.6f}"

    # Sign check: raw_dd <= 0 for all non-NaN cells (price <= its rolling max)
    # Use fillna(0) so NaN positions don't fail the comparison
    assert (raw_dd_full.fillna(0.0) <= 1e-6).values.all(), \
        "drawdown raw must be <= 0 everywhere (within float tolerance)"

    # Sign check: a coin with the MOST NEGATIVE raw drawdown on date t
    # should get the HIGHEST z-score (= most attractive LONG)
    row_raw = raw_dd_full.loc[t].dropna()
    most_down_coin = row_raw.idxmin()   # most negative raw -> cheapest
    row_z = dd90.loc[t].dropna()
    # The most-down coin should have a high (positive) z-score
    assert row_z[most_down_coin] == row_z.max() or row_z[most_down_coin] > 0, \
        (f"SIGN ERROR: most-drawn-down coin {most_down_coin} should have HIGH z-score "
         f"(got {row_z[most_down_coin]:+.3f}, max is {row_z.max():+.3f})")
    print(f"\n[drawdown_from_high(90)] @ {t.date()}:")
    print(f"  raw dd for {c}: {raw_dd_manual:+.4f}  (negative = below peak, correct)")
    print(f"  most-drawn-down coin: {most_down_coin}  z-score: {row_z[most_down_coin]:+.3f}  (POSITIVE = LONG)  OK")

    # ── Signal 1b: drawdown_from_high(expanding) ──────────────────────────────
    dd_exp = drawdown_from_high(P, window=None)  # expanding ATH
    # The expanding max should be >= price for all dates
    exp_max = price.expanding(min_periods=2).max()
    raw_dd_exp = price / exp_max - 1.0
    assert (raw_dd_exp.fillna(0.0) <= 1e-6).values.all(), \
        "expanding drawdown raw must be <= 0 (within float tolerance)"
    # A coin at all-time-low should get highest z-score
    row_raw_exp = raw_dd_exp.loc[t].dropna()
    most_down_exp = row_raw_exp.idxmin()
    row_z_exp = dd_exp.loc[t].dropna()
    assert row_z_exp[most_down_exp] > 0, \
        f"SIGN ERROR: ATH-drawdown most-down coin {most_down_exp} got z={row_z_exp[most_down_exp]:+.3f}"
    print(f"\n[drawdown_from_high(expanding)] @ {t.date()}:")
    print(f"  most-drawn-down-from-ATH coin: {most_down_exp}  z-score: {row_z_exp[most_down_exp]:+.3f}  (POSITIVE = LONG)  OK")

    # ── Signal 2: dist_from_ma(100) ───────────────────────────────────────────
    dma100 = dist_from_ma(P, ma_window=100)
    ma100 = price.rolling(100, min_periods=100).mean()
    raw_dist = (price / ma100 - 1.0)

    # Hand-check: a coin below its MA has raw_dist < 0 -> high z-score
    row_raw_dist = raw_dist.loc[t].dropna()
    most_below_ma = row_raw_dist.idxmin()   # most below MA = cheapest
    row_z_dma = dma100.loc[t].dropna()
    assert row_z_dma[most_below_ma] > 0, \
        (f"SIGN ERROR: most-below-MA coin {most_below_ma} should have positive z "
         f"(got {row_z_dma[most_below_ma]:+.3f})")
    print(f"\n[dist_from_ma(100)] @ {t.date()}:")
    print(f"  most-below-MA coin: {most_below_ma}  z-score: {row_z_dma[most_below_ma]:+.3f}  (POSITIVE = LONG)  OK")

    # Seam-safety: first ma_window-1 rows of BTC should be NaN
    assert dma100.loc[c_listed[:99], c].isna().all(), "first 99 BTC dma100 rows should be NaN"
    assert not np.isnan(dma100.loc[c_listed[99], c]), "row 99 should be defined"
    print(f"  seam-safe: first 99 BTC rows NaN, row 99 defined  OK")

    # ── Signal 3: long_term_reversal(120) ────────────────────────────────────
    ltr120 = long_term_reversal(P, lookback=120)
    raw_ret120 = price / price.shift(120) - 1.0

    # Hand-check one cell
    t2 = c_listed[-5]
    i2 = price.index.get_loc(t2)
    t2_lag = price.index[i2 - 120]
    manual_ret = price.loc[t2, c] / price.loc[t2_lag, c] - 1.0
    assert np.isclose(raw_ret120.loc[t2, c], manual_ret), \
        f"ltr raw mismatch: {raw_ret120.loc[t2, c]} != {manual_ret}"

    # Sign check: past loser (low raw_ret) should get high z-score
    row_raw_ret = raw_ret120.loc[t2].dropna()
    past_loser = row_raw_ret.idxmin()   # worst 120d return
    row_z_ltr = ltr120.loc[t2].dropna()
    assert row_z_ltr[past_loser] > 0, \
        (f"SIGN ERROR: past loser {past_loser} should have positive z-score "
         f"(got {row_z_ltr[past_loser]:+.3f})")
    print(f"\n[long_term_reversal(120)] @ {t2.date()}:")
    print(f"  past loser ({past_loser}, raw={row_raw_ret[past_loser]:+.3f}): "
          f"z-score {row_z_ltr[past_loser]:+.3f}  (POSITIVE = LONG)  OK")

    # Seam-safety: first 120 rows of BTC should be NaN
    assert ltr120.loc[c_listed[:120], c].isna().all(), "first 120 BTC ltr120 rows should be NaN"
    print(f"  seam-safe: first 120 BTC rows NaN  OK")

    # ── Summary: top/bottom-5 per signal at the last available date ──────────
    print(f"\n=== SNAPSHOT (most recent available date) ===")
    for name, sig in [
        ("dd90",    drawdown_from_high(P, window=90)),
        ("dd180",   drawdown_from_high(P, window=180)),
        ("dd_exp",  drawdown_from_high(P, window=None)),
        ("dma100",  dist_from_ma(P, ma_window=100)),
        ("dma200",  dist_from_ma(P, ma_window=200)),
        ("ltr120",  long_term_reversal(P, lookback=120)),
        ("ltr180",  long_term_reversal(P, lookback=180)),
    ]:
        d = sig.dropna(how="all").index[-1]
        row = sig.loc[d].dropna().sort_values(ascending=False)
        top = "  ".join(f"{cc}{row[cc]:+.2f}" for cc in row.index[:5])
        bot = "  ".join(f"{cc}{row[cc]:+.2f}" for cc in row.index[-5:])
        print(f"  {name:<10} @ {d.date()}  top5(LONG): {top}")
        print(f"  {'':<10}               bot5(SHORT): {bot}")

    print("\nALL ASSERTS PASSED")
