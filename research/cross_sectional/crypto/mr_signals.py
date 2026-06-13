"""
Time-series mean-reversion signals for the crypto cross-sectional book.

DISTINCT from the cross-sectional value/reversal signals already in signals.py:
  - signals.py reversal(lb) = -(price[t]/price[t-lb]-1): a CROSS-SECTIONAL
    ranking signal (each coin is ranked vs the others at the same lb).
  - HERE: each coin is z-scored vs its OWN rolling history (per-asset).

Per-asset seam-safe z-score:
  ts_z(price, window)[t, c]
    = (price[t,c] - rolling_mean(price[:,c], window, min_periods=window)[t])
      / rolling_std(price[:,c], window, min_periods=window)[t]

Mean-reversion signal: -ts_z  (z > 0 = overbought → short; z < 0 = oversold → long)

Sign invariant: a coin trading BELOW its rolling mean has z < 0, so the MR
signal (-z) > 0 → POSITIVE (long). Hand-checked in __main__.

SEAM-SAFETY: rolling_{mean,std} with window w and min_periods=w uses ONLY price
data at index positions <= t (pandas rolling is look-back, not centred). The first
w-1 rows of each coin's listed span are NaN (insufficient history). fwd_ret is
NEVER read here.

Alternative formulation — normalized RSI-flavour distance:
  rsi_oversold_score[t, c] = -(price_sma_ratio[t, c] - 1)
  where price_sma_ratio[t, c] = price[t, c] / rolling_mean(price[:, c], window) - 1.
  This is equivalent to the raw (price - MA) deviation without dividing by std;
  it is a signed-distance signal rather than a volatility-normalized z-score.
  Useful as robustness check: if both z-score MR and raw-distance MR agree the
  edge is there (or isn't), the result is not an artefact of the normalization.
"""

import numpy as np
import pandas as pd


def ts_zscore(panel: dict | pd.DataFrame, window: int) -> pd.DataFrame:
    """Per-asset time-series z-score of price over a rolling window.

    z[t, c] = (price[t, c] - mean(price[t-w+1..t, c])) / std(price[t-w+1..t, c])

    min_periods=window: rows where the coin doesn't yet have a full window's
    worth of price history are NaN. ddof=0 (population std, consistent with
    cross-sectional z-scoring in zscore_cross_section).

    NOT a cross-sectional z-score: each coin is standardized against its own
    history, not relative to the other coins at date t.
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    roll = price.rolling(window, min_periods=window)
    mean = roll.mean()
    std = roll.std(ddof=0).replace(0.0, np.nan)
    return (price - mean) / std


def ts_mr_signal(panel: dict | pd.DataFrame, window: int) -> pd.DataFrame:
    """MR signal: -ts_zscore.

    Positive (long) when price < rolling mean (oversold); negative (short) when
    price > rolling mean (overbought). NaN where ts_zscore is NaN.
    """
    return -ts_zscore(panel, window)


def ts_mr_raw_distance(panel: dict | pd.DataFrame, window: int) -> pd.DataFrame:
    """Alternative MR signal: negative of (price/SMA - 1), un-normalized.

    raw[t, c] = -(price[t,c] / rolling_mean(price[:,c], window) - 1)

    This is the signed percentage distance from the moving average, negated for
    the long-low / short-high MR convention. Not divided by std, so it is scale-
    dependent across coins (a high-vol coin shows bigger deviations) but still
    per-asset time-series. Useful as a robustness check alongside ts_mr_signal.
    min_periods=window for consistency; NaN before the window fills.
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    ma = price.rolling(window, min_periods=window).mean().replace(0.0, np.nan)
    return -(price / ma - 1.0)


def normalize_weights(raw_pos: pd.DataFrame, target_gross: float = 2.0) -> pd.DataFrame:
    """Normalize arbitrary per-asset positions so the daily gross Σ|w| = target_gross.

    Each row: w[t] = raw[t] * (target_gross / Σ|raw[t]|). Rows with no valid
    positions (all-NaN or zero gross) get a zero row (no trade).

    This preserves the SIGN and SHAPE of positions; it only rescales the
    magnitude so the gross leverage is comparable across days and to the
    tercile book (which has Σ|w|=2 by construction).
    """
    w = raw_pos.fillna(0.0)
    gross = w.abs().sum(axis=1)
    gross = gross.replace(0.0, np.nan)
    scale = target_gross / gross
    return w.mul(scale, axis=0).fillna(0.0)


def beta_neutral_weights(raw_pos: pd.DataFrame, target_gross: float = 2.0) -> pd.DataFrame:
    """Cross-sectionally demean positions each day, then normalize gross.

    Step 1: subtract the row mean (across all coins) to make Σw = 0 (beta-neutral).
    Step 2: normalize gross Σ|w| = target_gross.

    The demeaning removes the systematic long or short bias that the raw TS-MR
    signal imparts when 'most coins are below their MA' (which in a bull run they
    wouldn't be, but in a bear/range regime many can be simultaneously oversold).
    The resulting book is dollar-neutral and isolates pure reversion alpha from
    the underlying crypto beta.
    """
    w = raw_pos.fillna(0.0)
    row_mean = w.mean(axis=1)
    w_dm = w.sub(row_mean, axis=0)   # subtract row mean → Σw = 0 per row
    gross = w_dm.abs().sum(axis=1).replace(0.0, np.nan)
    scale = target_gross / gross
    return w_dm.mul(scale, axis=0).fillna(0.0)


# ── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/research/cross_sectional/crypto")
    import cryptodata

    # ── (1) Seam-safety structural check ──────────────────────────────────────
    P = cryptodata.load_panel()
    price = P["price"]
    print(f"\n=== PANEL ===  {price.shape[0]} days x {price.shape[1]} coins  "
          f"({price.index.min().date()} -> {price.index.max().date()})")

    WINDOW = 10
    z = ts_zscore(P, WINDOW)
    mr = ts_mr_signal(P, WINDOW)
    raw_d = ts_mr_raw_distance(P, WINDOW)

    # (a) First WINDOW-1 rows of a fully-listed coin must be NaN.
    c = "BTC"
    btc_listed = price[c].dropna().index
    head_z = z.loc[btc_listed[:WINDOW - 1], c]
    assert head_z.isna().all(), f"first {WINDOW-1} listed rows of z must be NaN"
    assert not np.isnan(z.loc[btc_listed[WINDOW - 1], c]), \
        f"row {WINDOW-1} (index {WINDOW-1}) should be defined"
    print(f"[seam-safety] first {WINDOW-1} listed BTC z-score rows NaN, row {WINDOW-1} defined  OK")

    # (b) Hand-compute one cell and assert match.
    t = btc_listed[WINDOW + 5]
    window_prices = price.loc[btc_listed[:WINDOW + 6], c].iloc[-WINDOW:]
    assert len(window_prices) == WINDOW, "window size check"
    man_mean = window_prices.mean()
    man_std = window_prices.std(ddof=0)
    man_z = (price.loc[t, c] - man_mean) / man_std
    assert np.isclose(z.loc[t, c], man_z, atol=1e-10), \
        f"ts_zscore cell mismatch: {z.loc[t,c]:.6f} vs {man_z:.6f}"
    print(f"[hand-check] z[{t.date()}, {c}] = {z.loc[t,c]:+.6f}  == manual {man_z:+.6f}  OK")

    # (c) KEY SIGN INVARIANT: a coin well below its rolling mean → MR signal > 0 (LONG).
    # Force a date where price < rolling mean for BTC.
    z_btc = z[c].dropna()
    # Find the most oversold date for BTC (most negative z)
    most_oversold_t = z_btc.idxmin()
    assert z_btc.loc[most_oversold_t] < 0, "oversold should have negative z"
    assert mr.loc[most_oversold_t, c] > 0, \
        f"MR signal at oversold date must be POSITIVE (long), got {mr.loc[most_oversold_t, c]:.4f}"
    print(f"[sign] BTC most oversold date {most_oversold_t.date()}: "
          f"z={z_btc.loc[most_oversold_t]:+.4f}, MR_signal={mr.loc[most_oversold_t, c]:+.4f}  "
          f"(POSITIVE = long, correct)  OK")

    # Find the most overbought date for BTC (most positive z)
    most_overbought_t = z_btc.idxmax()
    assert z_btc.loc[most_overbought_t] > 0, "overbought should have positive z"
    assert mr.loc[most_overbought_t, c] < 0, \
        f"MR signal at overbought date must be NEGATIVE (short), got {mr.loc[most_overbought_t, c]:.4f}"
    print(f"[sign] BTC most overbought date {most_overbought_t.date()}: "
          f"z={z_btc.loc[most_overbought_t]:+.4f}, MR_signal={mr.loc[most_overbought_t, c]:+.4f}  "
          f"(NEGATIVE = short, correct)  OK")

    # (d) mr = -z exactly
    assert np.allclose(mr.fillna(0.0).values, -z.fillna(0.0).values), \
        "ts_mr_signal must equal -ts_zscore"
    print("[identity] ts_mr_signal == -ts_zscore  OK")

    # (e) raw distance sign: same direction as z-score MR on meaningful signals.
    # Both signals derive from (price - MA): z-score divides by std; raw-distance
    # doesn't. Since (price - MA) has the same sign in both formulations, they
    # MUST agree wherever both are nonzero. NaN cells are excluded.
    mr_flat   = mr.fillna(0.0).values.ravel()
    rd_flat   = raw_d.fillna(0.0).values.ravel()
    both_nz   = (mr_flat != 0) & (rd_flat != 0)
    agree = float((np.sign(mr_flat[both_nz]) == np.sign(rd_flat[both_nz])).mean())
    print(f"[robustness] z-score MR and raw-distance MR sign agreement (both nonzero): {agree:.1%}  "
          f"(must be 100% — same (price-MA) direction, different normalization)  OK")
    assert agree > 0.999, f"sign agreement should be ~100% where both nonzero: {agree:.4f}"

    # ── (2) normalize_weights sanity ──────────────────────────────────────────
    TARGET_GROSS = 2.0
    mr_full = ts_mr_signal(P, WINDOW)
    w_raw = normalize_weights(mr_full, target_gross=TARGET_GROSS)
    gross_per_day = w_raw.abs().sum(axis=1)
    # Only check days where we actually have positions
    active = gross_per_day[gross_per_day > 0]
    assert np.allclose(active.values, TARGET_GROSS, atol=1e-9), \
        f"gross != {TARGET_GROSS} on active days: max dev {(active - TARGET_GROSS).abs().max():.2e}"
    print(f"[normalize] gross Σ|w| = {TARGET_GROSS} on all active days  OK")

    # ── (3) beta_neutral_weights sanity ─────────────────────────────────────
    w_bn = beta_neutral_weights(mr_full, target_gross=TARGET_GROSS)
    net_per_day = w_bn.sum(axis=1)
    gross_bn = w_bn.abs().sum(axis=1)
    active_bn = gross_bn[gross_bn > 0]
    # Net should be ~0 (dollar-neutral)
    assert np.allclose(net_per_day[gross_bn > 0].values, 0.0, atol=1e-9), \
        f"beta-neutral net != 0: max |net| = {net_per_day[gross_bn>0].abs().max():.2e}"
    assert np.allclose(active_bn.values, TARGET_GROSS, atol=1e-9), \
        f"beta-neutral gross != {TARGET_GROSS}: max dev {(active_bn-TARGET_GROSS).abs().max():.2e}"
    print(f"[beta-neutral] Σw = 0 and gross = {TARGET_GROSS} on all active days  OK")

    # ── (4) NaN propagation: fwd_ret never touched ────────────────────────────
    assert "fwd_ret" in P, "panel must carry fwd_ret"
    orig_fwd = P["fwd_ret"].copy()
    _ = ts_mr_signal(P, WINDOW)
    _ = ts_mr_raw_distance(P, WINDOW)
    assert P["fwd_ret"].equals(orig_fwd), "fwd_ret must not be modified by MR signals"
    print("[no-look-ahead] fwd_ret untouched by MR signals  OK")

    # ── (5) Eyeball: which coins are most oversold/overbought TODAY ─────────
    latest_t = z.dropna(how="all").index[-1]
    z_row = z.loc[latest_t].dropna().sort_values()
    print(f"\n=== LATEST MR SIGNALS @ {latest_t.date()} (window={WINDOW}) ===")
    print(f"  Most OVERSOLD  (MR signal = LONG, z most negative):")
    for cc in z_row.index[:5]:
        print(f"    {cc:<8} z={z_row[cc]:+.3f}  MR={-z_row[cc]:+.3f}")
    print(f"  Most OVERBOUGHT (MR signal = SHORT, z most positive):")
    for cc in z_row.index[-5:]:
        print(f"    {cc:<8} z={z_row[cc]:+.3f}  MR={-z_row[cc]:+.3f}")

    print("\nALL ASSERTS PASSED")
