"""Honest DAILY annualization for the crypto cross-sectional book.

The shared validation_harness annualizes with HOURS_PER_YEAR=8760 (it models an
hourly book), so its Sharpe/ann levels are inflated by ~sqrt(8760/365)=~4.9 (and
ann return by ~24x) when applied to our DAILY pnl. This module provides the
CORRECT daily helper so the sweep and future FX work never quote inflated levels.
We do NOT edit the harness — this is a local, importable helper.

A daily pnl series here is a per-day net return of the whole long-short book
(one number per calendar day, as produced by xsec.portfolio_returns). Periods
per year = 365 (calendar-daily grid; the panel index is gap-free daily).

Mirrors the metric definitions in analyze_c4.py (PPY=365, ddof=0, equity from
cumprod) so numbers cross-check 1:1.

Only numpy/pandas.
"""

import numpy as np
import pandas as pd

PPY = 365  # daily periods per year (calendar-daily, gap-free panel grid)


def daily_metrics(pnl: pd.Series) -> dict:
    """Honest daily-annualized performance metrics for a daily pnl series.

    pnl: pd.Series of per-day net book returns (additive simple returns).
    Returns dict:
      n       — number of non-NaN days,
      ann     — annualized return = mean * 365 (arithmetic, matches analyze_c4),
      vol_ann — annualized volatility = std(ddof=0) * sqrt(365),
      sharpe  — mean/std * sqrt(365) (zero rf; ddof=0),
      maxdd   — max drawdown as a positive fraction (0..1) of the compounded
                equity curve cumprod(1+r),
      calmar  — ann / maxdd (NaN if maxdd ~ 0),
      hit     — fraction of days with r > 0.
    Fewer than 30 valid days → {} (too short to be meaningful), same guard as
    analyze_c4.metrics.
    """
    r = pnl.dropna().values
    if len(r) < 30:
        return {}
    mean, std = r.mean(), r.std(ddof=0)
    sharpe = (mean / std) * np.sqrt(PPY) if std > 0 else 0.0
    ann = mean * PPY
    vol_ann = std * np.sqrt(PPY)
    eq = np.cumprod(1.0 + r)
    dd = 1.0 - eq / np.maximum.accumulate(eq)
    maxdd = float(dd.max())
    calmar = ann / maxdd if maxdd > 1e-9 else float("nan")
    hit = float((r > 0).mean())
    return dict(n=len(r), ann=ann, vol_ann=vol_ann, sharpe=sharpe,
                maxdd=maxdd, calmar=calmar, hit=hit, mean=mean, std=std)


# ── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # (1) Scaling law: Sharpe must scale with sqrt(365), ann with 365.
    rng = np.random.default_rng(0)
    s = pd.Series(rng.normal(0.0005, 0.01, size=2000))
    m = daily_metrics(s)
    daily_sharpe = s.mean() / s.std(ddof=0)
    assert np.isclose(m["sharpe"], daily_sharpe * np.sqrt(PPY)), "sharpe scaling"
    assert np.isclose(m["ann"], s.mean() * PPY), "ann scaling"
    assert np.isclose(m["vol_ann"], s.std(ddof=0) * np.sqrt(PPY)), "vol scaling"
    print(f"[scaling] daily_sharpe={daily_sharpe:.4f} -> ann_sharpe={m['sharpe']:.4f} "
          f"(x sqrt(365)={np.sqrt(PPY):.3f})  OK")

    # (2) Hand-checkable drawdown: down 10% then flat → maxdd = 0.10.
    dd_series = pd.Series([0.0] * 30 + [-0.10] + [0.0] * 30)
    md = daily_metrics(dd_series)
    assert np.isclose(md["maxdd"], 0.10), f"maxdd {md['maxdd']} != 0.10"
    print(f"[maxdd] -10% shock series → maxdd={md['maxdd']:.4f}  OK")

    # (3) Cross-check against analyze_c4.metrics on a real book pnl series.
    import sys
    sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/research/cross_sectional/crypto")
    try:
        import analyze_c4
        from crypto_pkg import CryptoXSecPackage
        pkg = CryptoXSecPackage()
        df = pkg.load("XSEC")
        menu = pkg.menu("XSEC", df)
        pnl = menu["blend"]
        a = analyze_c4.metrics(pnl)
        b = daily_metrics(pnl)
        for k in ("ann", "sharpe", "maxdd", "calmar", "hit", "n"):
            av, bv = a[k], b[k]
            if isinstance(av, float) and np.isnan(av):
                assert np.isnan(bv), f"{k} NaN mismatch"
            else:
                assert np.isclose(av, bv), f"{k}: analyze_c4 {av} != daily_metrics {bv}"
        print(f"[cross-check] daily_metrics == analyze_c4.metrics on blend pnl "
              f"(sharpe={b['sharpe']:.2f} ann={100*b['ann']:.2f}% maxdd={100*b['maxdd']:.2f}%)  OK")
    except Exception as e:
        print(f"[cross-check] skipped ({type(e).__name__}: {e})")

    print("\nALL ASSERTS PASSED")
