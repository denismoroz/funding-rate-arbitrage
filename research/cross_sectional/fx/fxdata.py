"""
Cross-sectional G10 FX data layer — universe + aligned daily panel loader for a
long-short book of 9 G10 currencies vs USD (USD is the numeraire). Analogous in
spirit to crypto/cryptodata.py.

Universe (fixed G10, USD excluded as the numeraire):
  EUR, JPY, GBP, CHF, AUD, NZD, CAD, NOK, SEK
All spot normalized to the XXXUSD orientation = "USD per 1 unit of foreign
currency": price up => foreign currency strengthens vs USD. Per-pair inversion
is applied where the raw source quotes USDxxx (JPY/CHF/CAD/NOK/SEK) — see
fetch.INVERT.

Three data layers, each its own loader, combined by load_panel():
  1. spot  (Stooq->Yahoo)   -> price / fwd_ret      (momentum + return stream)
  2. rates (FRED 3M IB)     -> short_rate / usd_rate (carry = rate differential)
  3. REER  (BIS broad real) -> reer                  (value)

All external URLs + download dates are documented in fetch.py.

Panels (load_panel) are pd.DataFrame indexed by a regular daily UTC business-day
DatetimeIndex, one column per currency (the 9 above), columns aligned. NaN before
a series starts is acceptable (like crypto's pre-listing NaN), but the COMMON
window where spot exists for all 9 is NaN-free in price/fwd_ret.

  price      — spot XXXUSD daily close.
  fwd_ret    — next-day forward simple return price.shift(-1)/price - 1, so
               fwd_ret[t] is the t->t+1 return. Matches xsec.portfolio_returns'
               alignment contract: weight at t earns fwd_ret[t] (caller aligns).
  short_rate — annualized foreign short rate in PERCENT, ffilled to daily.
  usd_rate   — the USD short rate in PERCENT (a single-column DataFrame 'USD'),
               ffilled to daily; broadcast against short_rate for the carry diff.
  reer       — REER broad real index per currency, ffilled to daily.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import fetch

DATA_DIR = fetch.DATA_DIR
CURRENCIES = fetch.CURRENCIES

# Business-day grid in UTC. FX trades Mon-Fri; spot sources already exclude
# weekends, so we align to a regular business-day index over the common window.
FREQ = "B"


# ── Per-layer loaders (read from cache; fetch.fetch_all populates it) ──────────

def _load_spot(ccy: str) -> pd.Series:
    """Cached raw spot -> XXXUSD oriented close Series (date index, UTC)."""
    df = pd.read_csv(DATA_DIR / f"spot_{ccy}.csv")
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
    s = df.set_index("date")["close"].astype(float).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if fetch.INVERT[ccy]:
        s = 1.0 / s  # USDxxx (foreign per USD) -> XXXUSD (USD per foreign)
    return s


def _load_rate(ccy: str) -> pd.Series:
    """Cached FRED short rate (% p.a.) -> Series(date index, UTC)."""
    df = pd.read_csv(DATA_DIR / f"rate_{ccy}.csv")
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
    return df.set_index("date")["rate"].astype(float).sort_index()


def _load_reer(ccy: str) -> pd.Series:
    """Cached BIS REER (broad real index) -> Series(date index, UTC)."""
    df = pd.read_csv(DATA_DIR / f"reer_{ccy}.csv")
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
    return df.set_index("date")["reer"].astype(float).sort_index()


# ── Combiner ──────────────────────────────────────────────────────────────────

def load_panel(refresh: bool = False) -> dict:
    """Aligned daily panels for the 9-currency G10 universe.

    refresh=True forces a re-download of all raw CSVs first; otherwise reads the
    existing cache (and fetches only missing files).

    Returns dict:
      currencies — list[str] (the 9)
      price      — DataFrame[date x ccy]  spot XXXUSD close
      fwd_ret    — DataFrame[date x ccy]  next-day forward return (t->t+1 at t)
      short_rate — DataFrame[date x ccy]  foreign 3M rate, % p.a., ffilled daily
      usd_rate   — DataFrame[date x 'USD'] USD 3M rate, % p.a., ffilled daily
      reer       — DataFrame[date x ccy]  REER broad real index, ffilled daily
    All frames share one regular UTC business-day index. NaN before a series
    starts; price/fwd_ret are NaN-free over the common (all-9-present) window.
    """
    # Ensure cache exists (fetch_all skips files that already exist).
    need = any(not (DATA_DIR / f"spot_{c}.csv").exists() for c in CURRENCIES)
    need = need or any(not (DATA_DIR / f"rate_{c}.csv").exists() for c in ["USD", *CURRENCIES])
    need = need or any(not (DATA_DIR / f"reer_{c}.csv").exists() for c in CURRENCIES)
    if refresh or need:
        fetch.fetch_all(refresh=refresh)

    spot = {c: _load_spot(c) for c in CURRENCIES}
    rate = {c: _load_rate(c) for c in CURRENCIES}
    reer = {c: _load_reer(c) for c in CURRENCIES}
    usd = _load_rate("USD")

    price_raw = pd.DataFrame(spot).sort_index()

    # Common window = where ALL 9 spot series are present (price NaN-free there).
    common_start = max(s.dropna().index.min() for s in spot.values())
    common_end = min(s.dropna().index.max() for s in spot.values())

    # Regular business-day UTC index over the common window.
    idx = pd.date_range(common_start, common_end, freq=FREQ, tz="UTC")

    # price: align to the business-day grid, ffill small spot gaps (holidays) so
    # the common window is gap-free.
    price = price_raw.reindex(price_raw.index.union(idx)).ffill().reindex(idx)
    price = price[CURRENCIES]

    # forward return r_{t+1}=P_{t+1}/P_t - 1, indexed at t (last row NaN).
    fwd_ret = price.shift(-1) / price - 1.0

    # rates/reer: align to grid and forward-fill (monthly -> daily). Reindex onto
    # the union first so a value dated before the grid start still ffills in.
    def _ffill_to_grid(d: dict[str, pd.Series]) -> pd.DataFrame:
        df = pd.DataFrame(d).sort_index()
        return df.reindex(df.index.union(idx)).ffill().reindex(idx)[list(d.keys())]

    short_rate = _ffill_to_grid(rate)
    reer_df = _ffill_to_grid(reer)
    usd_df = _ffill_to_grid({"USD": usd})

    return {
        "currencies": CURRENCIES,
        "price": price,
        "fwd_ret": fwd_ret,
        "short_rate": short_rate,
        "usd_rate": usd_df,
        "reer": reer_df,
    }


# ── Self-test / sanity prints ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    refresh = "--refresh" in sys.argv

    P = load_panel(refresh=refresh)
    price, fwd = P["price"], P["fwd_ret"]
    short_rate, usd_rate, reer = P["short_rate"], P["usd_rate"], P["reer"]

    print(f"\n=== PANEL (G10 vs USD, XXXUSD orientation) ===")
    print(f"common window : {price.index.min().date()} -> {price.index.max().date()}"
          f"  (n_days={len(price)})")
    step = price.index.to_series().diff().dropna().dt.days
    print(f"index step    : min={step.min()}d max={step.max()}d (business-day grid)")
    print(f"price NaN in window  : {int(price.isna().sum().sum())} (must be 0)")
    print(f"fwd_ret NaN (ex last): {int(fwd.iloc[:-1].isna().sum().sum())} (must be 0)")

    print(f"\n{'ccy':<5}{'first':>12}{'last':>12}{'nobs':>8}{'mean_spot':>14}"
          f"{'invert':>8}")
    for c in CURRENCIES:
        s = price[c].dropna()
        print(f"{c:<5}{str(s.index.min().date()):>12}{str(s.index.max().date()):>12}"
              f"{len(s):>8}{s.mean():>14.5f}{str(fetch.INVERT[c]):>8}")

    # ── Carry sanity: mean short-rate differential (foreign - USD) ───────────
    print(f"\n=== CARRY SANITY: mean (foreign - USD) 3M rate, % p.a. ===")
    print("  high-yielders (AUD/NZD/NOK) ~positive; low-yielders (JPY/CHF) ~negative")
    usd_s = usd_rate["USD"]
    diffs = {}
    for c in CURRENCIES:
        d = (short_rate[c] - usd_s).dropna()
        diffs[c] = d.mean()
    print(f"\n{'ccy':<5}{'mean_rate%':>12}{'mean_USD%':>12}{'diff(f-USD)%':>16}")
    for c in CURRENCIES:
        print(f"{c:<5}{short_rate[c].mean():>12.3f}{usd_s.mean():>12.3f}"
              f"{diffs[c]:>16.3f}")
    hi = [c for c in ["AUD", "NZD", "NOK"] if diffs[c] > 0]
    lo = [c for c in ["JPY", "CHF"] if diffs[c] < 0]
    print(f"  high-yielders positive: {hi}   low-yielders negative: {lo}")

    # ── Orientation sanity ───────────────────────────────────────────────────
    print(f"\n=== ORIENTATION SANITY (all XXXUSD) ===")
    eur_last = price["EUR"].dropna().iloc[-1]
    jpy_last = price["JPY"].dropna().iloc[-1]
    print(f"  EURUSD last = {eur_last:.4f}  (expect ~1.0-1.2)")
    print(f"  JPYUSD last = {jpy_last:.6f}  (expect ~0.006-0.01, NOT ~100+)")
    assert jpy_last < 0.1, f"JPYUSD not inverted: {jpy_last}"
    assert 0.8 < eur_last < 1.6, f"EURUSD out of range: {eur_last}"
    print("  guards passed: JPYUSD < 0.1 and 0.8 < EURUSD < 1.6")

    # ── REER sanity ──────────────────────────────────────────────────────────
    print(f"\n=== REER SANITY (broad real index) ===")
    rr = reer.dropna(how="all")
    print(f"  reer window : {rr.index.min().date()} -> {rr.index.max().date()}")
    rvals = reer.values[~np.isnan(reer.values)]
    print(f"  value range : {rvals.min():.1f} .. {rvals.max():.1f} "
          f"(expect positive index levels ~50-150)")
    assert (rvals > 0).all(), "REER has non-positive levels"
    print(f"\n{'ccy':<5}{'reer_first':>12}{'reer_last':>12}")
    for c in CURRENCIES:
        s = reer[c].dropna()
        print(f"{c:<5}{s.iloc[0]:>12.2f}{s.iloc[-1]:>12.2f}")

    print("\nALL SANITY CHECKS PASSED")
