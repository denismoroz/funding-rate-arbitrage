"""
Cross-sectional factor signals for the crypto long-short book.

Each function consumes the aligned daily panel from cryptodata.load_panel() (or
the relevant frame) and returns a SCORE panel pd.DataFrame[date x coin] where
HIGHER = more attractive to LONG, NaN where history is insufficient. Scores feed
xsec.rank_to_weights, which longs the top tercile / shorts the bottom tercile.

NO look-ahead: every signal at row t uses only data with index <= t. fwd_ret is
NEVER read here (it is the realised forward return, the engine's job to align).
Seam-safe: signals are built on the FULL panel so lookback windows stay intact;
where a coin lacks enough listed history the cell stays NaN (never fabricated).

Only numpy/pandas.
"""

import numpy as np
import pandas as pd


def momentum(panel: dict, lookback_days: int) -> pd.DataFrame:
    """Trailing total return over `lookback_days`, score = higher → more attractive.

    score[t,c] = price[t,c] / price[t - lookback_days, c] - 1, using the close at
    t (known at t → NO look-ahead). The first `lookback_days` rows of each coin's
    listed span are NaN (not enough history). Because the panel index is a regular
    daily grid, `.shift(lookback_days)` is an exact `lookback_days`-calendar-day
    lag; NaNs in the lagged price (pre-listing) propagate, so cells stay NaN until
    a full window of listed prices exists.
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    return price / price.shift(lookback_days) - 1.0


def carry(panel: dict, smooth_days: int = 14) -> pd.DataFrame:
    """Smoothed recent funding, score = higher → more attractive to LONG.

    score[t,c] = - mean( funding[t-smooth_days+1 .. t, c] ), the negated trailing
    mean of daily summed HL funding over a window ending at t (data <= t only).

    Sign convention (DIRECTIONAL book, NOT delta-neutral): positive HL funding
    means longs PAY shorts, so a coin with high positive funding is expensive /
    crowded to be long (you bleed carry holding the long), while low or negative
    funding means being long is cheap or even paid. We therefore want HIGHER score
    (more attractive long) for LOWER funding, hence the leading minus: the book
    goes long the cheapest-to-carry coins and shorts the priciest-to-carry ones,
    which also harvests the funding spread on the short leg.

    `min_periods=smooth_days` ⇒ the first `smooth_days-1` listed rows of each coin
    are NaN. Funding is already NaN outside a coin's listed span (cryptodata masks
    it by price.notna()), so pre-listing rows stay NaN.
    """
    funding = panel["funding"] if isinstance(panel, dict) else panel
    smoothed = funding.rolling(smooth_days, min_periods=smooth_days).mean()
    return -smoothed


def zscore_cross_section(scores: pd.DataFrame) -> pd.DataFrame:
    """Standardize each ROW (date) across coins: (x - row_mean) / row_std.

    Mean/std are taken over the non-NaN coins of that date (ddof=0, population).
    NaNs are preserved. Rows with <2 valid coins or zero cross-sectional spread
    (std==0) yield NaN (undefined standardization → no information that date).
    Output is unit-scaled so different factors are comparable for blending.
    """
    mean = scores.mean(axis=1)
    std = scores.std(axis=1, ddof=0)
    z = scores.sub(mean, axis=0).div(std.replace(0.0, np.nan), axis=0)
    return z


def blend(panels_or_scores, weights=None) -> pd.DataFrame:
    """Weighted average of per-factor z-scored panels → one blended score.

    panels_or_scores: list of pd.DataFrame factor panels (ALREADY z-scored, so the
                      legs are comparable; pass them through zscore_cross_section).
    weights: per-factor weights (any positive scale; normalized to sum 1). Default
             equal weights.

    Frames are aligned on the UNION of index/columns. At each cell the blend is the
    weight-renormalized average over the factors that are non-NaN there (a missing
    factor is dropped from that cell's average, not treated as zero); a cell with
    NO valid factor stays NaN. Higher = more attractive long, by construction.
    """
    panels = list(panels_or_scores)
    if not panels:
        raise ValueError("blend: need at least one factor panel")
    if weights is None:
        weights = [1.0] * len(panels)
    if len(weights) != len(panels):
        raise ValueError("blend: weights length must match number of panels")
    w = np.asarray(weights, dtype=float)
    if (w < 0).any() or w.sum() == 0:
        raise ValueError("blend: weights must be non-negative and not all zero")

    idx = panels[0].index
    cols = panels[0].columns
    for p in panels[1:]:
        idx = idx.union(p.index)
        cols = cols.union(p.columns)
    panels = [p.reindex(index=idx, columns=cols) for p in panels]

    num = pd.DataFrame(0.0, index=idx, columns=cols)
    den = pd.DataFrame(0.0, index=idx, columns=cols)
    for wi, p in zip(w, panels):
        valid = p.notna()
        num = num.add((p.where(valid) * wi).fillna(0.0))
        den = den.add(valid.astype(float) * wi)
    return num.div(den.replace(0.0, np.nan))


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/research/cross_sectional/crypto")
    import cryptodata

    P = cryptodata.load_panel()
    price, funding = P["price"], P["funding"]
    print(f"\n=== PANEL ===  {price.shape[0]} days x {price.shape[1]} coins  "
          f"({price.index.min().date()} -> {price.index.max().date()})")

    # ── No-look-ahead structural checks ───────────────────────────────────────
    LB = 60
    mom = momentum(P, LB)

    # (a) hand-recompute one coin / one date from raw price, assert match.
    c = "BTC"
    t = price[c].dropna().index[-3]               # a recent, fully-listed date
    t0 = price.index[price.index.get_loc(t) - LB] # exactly LB calendar days back
    manual = price.loc[t, c] / price.loc[t0, c] - 1.0
    assert np.isclose(mom.loc[t, c], manual), f"momentum mismatch {mom.loc[t,c]} != {manual}"
    assert t0 == t - pd.Timedelta(days=LB), "index not a regular daily grid"
    print(f"\n[no-look-ahead] {c} momentum({LB}) @ {t.date()} = {mom.loc[t,c]:+.4f}  "
          f"== manual price[t]/price[t-{LB}d]-1 ({manual:+.4f})  OK")

    # (b) first LB listed rows of a long-history coin must be NaN.
    btc_listed = price[c].dropna().index
    head = mom.loc[btc_listed[:LB], c]
    assert head.isna().all(), "first LB rows of BTC momentum must be NaN"
    assert not np.isnan(mom.loc[btc_listed[LB], c]), "row LB should be defined"
    print(f"[no-look-ahead] first {LB} listed BTC rows are NaN, row {LB} is defined  OK")

    # (c) signals never touch fwd_ret: structurally true (functions take price/
    #     funding only); assert the panel still carries fwd_ret untouched.
    assert "fwd_ret" in P and mom.shape == price.shape
    print("[no-look-ahead] signals read price/funding only; fwd_ret untouched  OK")

    # ── Sanity: momentum top/bottom-5 vs realised 60d price move ───────────────
    car = carry(P)
    d = mom.dropna(how="all").index[-1]           # most recent date with momentum
    print(f"\n=== SANITY @ {d.date()} ===")

    mrow = mom.loc[d].dropna().sort_values(ascending=False)
    d0 = price.index[price.index.get_loc(d) - LB]
    print(f"\nmomentum({LB})  top-5 (should have RISEN over ~{LB}d):")
    print(f"  {'coin':<8}{'score':>10}{'price_t0':>12}{'price_t':>12}{'chk_ret':>10}")
    for cc in mrow.index[:5]:
        chk = price.loc[d, cc] / price.loc[d0, cc] - 1.0
        print(f"  {cc:<8}{mrow[cc]:>+10.4f}{price.loc[d0,cc]:>12.4g}{price.loc[d,cc]:>12.4g}{chk:>+10.4f}")
        assert np.isclose(mrow[cc], chk), "momentum top check"
    print(f"momentum({LB})  bottom-5 (should have FALLEN):")
    for cc in mrow.index[-5:]:
        chk = price.loc[d, cc] / price.loc[d0, cc] - 1.0
        print(f"  {cc:<8}{mrow[cc]:>+10.4f}{price.loc[d0,cc]:>12.4g}{price.loc[d,cc]:>12.4g}{chk:>+10.4f}")

    # carry: top score = LOWEST recent funding (cheapest to be long).
    crow = car.loc[d].dropna().sort_values(ascending=False)
    fmean = funding.rolling(14, min_periods=14).mean().loc[d]
    print(f"\ncarry(14)  top-5 (LOW/neg funding → cheap to long):")
    print(f"  {'coin':<8}{'score':>10}{'fund_mean14':>14}")
    for cc in crow.index[:5]:
        print(f"  {cc:<8}{crow[cc]:>+10.4f}{fmean[cc]:>14.2e}")
        assert np.isclose(crow[cc], -fmean[cc]), "carry == -mean(funding)"
    print(f"carry(14)  bottom-5 (HIGH funding → expensive to long, short these):")
    for cc in crow.index[-5:]:
        print(f"  {cc:<8}{crow[cc]:>+10.4f}{fmean[cc]:>14.2e}")
    # ordering must be exactly the negative-funding order
    assert (crow.values[:-1] >= crow.values[1:] - 1e-12).all(), "carry not sorted"
    assert crow.idxmax() == (-fmean).idxmax(), "carry top != lowest funding"
    print("[sanity] carry ordering == ascending recent-funding (sign convention)  OK")

    # ── Z-score: each row ~0 mean, ~unit std over non-NaN entries ──────────────
    z = zscore_cross_section(mom)
    rmean = z.mean(axis=1)
    rstd = z.std(axis=1, ddof=0)
    valid_rows = mom.notna().sum(axis=1) >= 2
    assert np.allclose(rmean[valid_rows].dropna(), 0.0, atol=1e-9), "z-score row mean != 0"
    bad = (rstd[valid_rows].dropna() - 1.0).abs()
    assert (bad < 1e-9).all(), f"z-score row std != 1 (max dev {bad.max():.2e})"
    print(f"\n=== Z-SCORE ===  rows checked: {int(valid_rows.sum())}  "
          f"max |mean|={rmean[valid_rows].abs().max():.2e}  "
          f"max |std-1|={bad.max():.2e}  OK")

    # ── Blend: equal-weight of z(momentum) + z(carry) ─────────────────────────
    bl = blend([zscore_cross_section(mom), zscore_cross_section(car)])
    print(f"\n=== BLEND (equal-weight z-mom + z-carry) ===  shape {bl.shape}")

    # ── Shapes & NaN% ─────────────────────────────────────────────────────────
    def nanpct(df):
        return 100.0 * df.isna().mean().mean()
    print(f"\n=== SHAPES & NaN% ===")
    for name, df in [("momentum(60)", mom), ("carry(14)", car),
                     ("zscore(mom)", z), ("blend", bl)]:
        print(f"  {name:<14} shape={str(df.shape):<14} NaN%={nanpct(df):5.1f}")

    print("\nALL ASSERTS PASSED")
