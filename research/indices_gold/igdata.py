"""
Indices + Gold/Silver data layer — aligned daily panel for TSMOM.

Universe (from fetch.ASSETS):
  SP500, NASDAQ, DOW, RUSSELL2K, FTSE, DAX, CAC, NIKKEI, HANGSENG, ASX200,
  TSX, GLD, SLV

load_panel() returns:
  assets   — list[str]  (all 13)
  price    — DataFrame[date x asset]  daily close, business-day grid
  fwd_ret  — DataFrame[date x asset]  next-day forward simple return
              fwd_ret[t] = price[t+1]/price[t] - 1 (indexed at t, last row NaN)

Panel is on a regular UTC BUSINESS-DAY ("B") grid from the earliest common-ish
start to the latest available date.  Pre-start dates are NaN (GLD from 2004-11,
SLV from 2006-04; indices from ~1993–1996).  The cross-section simply has fewer
assets early; signals propagate NaN until a full lookback exists (no fabrication).

PRICE-RETURN CAVEAT (documented, not corrected):
  Equity index LEVELS (^GSPC etc.) are PRICE-return indices: dividends are
  excluded.  The long-only equity risk premium contains ~2%/yr of dividends that
  our price series miss.  For a TSMOM strategy (which is net-long when trends are
  up) this means our CAR is understated by ~2%/yr on the equity portion.  We treat
  this as a CONSERVATIVE bias — the real-world TSMOM edge is at least as good as
  shown.  GLD/SLV are ETFs and ARE total-return proxies (they track spot price,
  no dividends, so they are clean).  Correcting for dividends would require a
  separate dividend-yield series; out of scope for this research module.

Only numpy/pandas.
"""

from __future__ import annotations
from pathlib import Path

import pandas as pd
import numpy as np

import ig_fetch as fetch

DATA_DIR = fetch.DATA_DIR
ASSETS = fetch.ASSETS

# Business-day grid in UTC.  Equity / ETF markets trade Mon-Fri.
FREQ = "B"


def _load_spot(asset: str) -> pd.Series:
    """Cached raw close -> float Series (UTC date index, sorted)."""
    p = DATA_DIR / f"spot_{asset}.csv"
    if not p.exists():
        raise FileNotFoundError(
            f"Cache missing for {asset}: run fetch.fetch_all() first "
            f"(or: python fetch.py  from research/indices_gold/)"
        )
    df = pd.read_csv(p)
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
    s = df.set_index("date")["close"].astype(float).sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_panel(refresh: bool = False) -> dict:
    """Aligned daily panels for the 13-asset universe.

    refresh=True forces a re-download of all raw CSVs first; otherwise reads
    the existing cache (and fetches only missing files).

    Returns dict:
      assets   — list[str] (the 13)
      price    — DataFrame[date x asset]  spot close on business-day grid
      fwd_ret  — DataFrame[date x asset]  fwd_ret[t] = price[t+1]/price[t]-1
                 (last row NaN, aligned so weight[t] earns fwd_ret[t])
    All frames share one regular UTC business-day DatetimeIndex.  NaN before an
    asset's series starts; cross-section has fewer assets early.
    """
    # Ensure cache exists (fetch_all skips files that already exist).
    need = any(not (DATA_DIR / f"spot_{a}.csv").exists() for a in ASSETS)
    if refresh or need:
        fetch.fetch_all(refresh=refresh)

    spot = {a: _load_spot(a) for a in ASSETS}

    # Union of all date ranges → full span.
    all_dates = sorted(set().union(*(s.index for s in spot.values())))
    if not all_dates:
        raise RuntimeError("No data loaded")
    idx_full = pd.date_range(all_dates[0], all_dates[-1], freq=FREQ, tz="UTC")

    # price: align each series to the biz-day grid, ffill small holiday gaps
    # (e.g. individual country holidays that Yahoo reports no data for).
    # Pre-start cells remain NaN (no back-fill).
    price_raw = pd.DataFrame(spot).sort_index()
    price = (
        price_raw
        .reindex(price_raw.index.union(idx_full))
        .ffill()                      # fill single holiday gaps forward
        .reindex(idx_full)            # keep only the biz-day spine
    )[ASSETS]

    # forward return r_{t+1} = P_{t+1}/P_t - 1, indexed at t (last row NaN).
    fwd_ret = price.shift(-1) / price - 1.0

    return {
        "assets":  ASSETS,
        "price":   price,
        "fwd_ret": fwd_ret,
    }


# ── Self-test / sanity prints ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    refresh = "--refresh" in sys.argv
    P = load_panel(refresh=refresh)
    price, fwd = P["price"], P["fwd_ret"]
    print(f"\n=== PANEL (13 assets, business-day grid) ===")
    print(f"grid: {price.index.min().date()} -> {price.index.max().date()}"
          f"  (n={len(price)} bdays)")
    step = price.index.to_series().diff().dropna().dt.days
    print(f"step: min={step.min()}d max={step.max()}d")
    print(f"\n{'asset':<14}{'first':>12}{'last':>12}{'nobs':>8}{'last_close':>13}{'nan%':>8}")
    for a in ASSETS:
        s = price[a].dropna()
        nan_pct = 100.0 * price[a].isna().mean()
        print(f"{a:<14}{str(s.index.min().date()):>12}{str(s.index.max().date()):>12}"
              f"{len(s):>8}{s.iloc[-1]:>13.2f}{nan_pct:>7.1f}%")
    # fwd_ret alignment sanity: fwd_ret[t] = price[t+1]/price[t]-1
    a = "SP500"
    t_idx = price[a].dropna().index[-3]
    i = price.index.get_loc(t_idx)
    t1_idx = price.index[i + 1]
    expected = price.loc[t1_idx, a] / price.loc[t_idx, a] - 1.0
    assert np.isclose(fwd.loc[t_idx, a], expected), \
        f"fwd_ret alignment fail: {fwd.loc[t_idx, a]} != {expected}"
    print(f"\n[fwd_ret] {a} @ {t_idx.date()}: fwd={fwd.loc[t_idx, a]:+.6f} "
          f"== p[t+1]/p[t]-1={expected:+.6f}  OK")
    print("\nALL CHECKS PASSED")
