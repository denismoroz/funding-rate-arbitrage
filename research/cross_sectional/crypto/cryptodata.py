"""
Cross-sectional crypto data layer — instrument universe + aligned panel loader
for a long-short book of Hyperliquid (HL) perps.

Universe is REPRODUCIBLE from filters (no hardcoded magic list):
  1. rank the full HL perp universe by 24h notional volume (metaAndAssetCtxs),
  2. exclude bridge tokens (name ends in a digit: AVAX0 / LINK0 / AAVE0 …),
  3. keep coins with >= MIN_VOL_USD 24h volume (liquidity),
  4. require >= MIN_HISTORY_DAYS of BOTH HL funding history AND hourly OHLCV
     (excludes brand-new / recently-relisted listings, e.g. ZEC/XMR/ICP on HL),
  5. require fresh, complete OHLCV (last candle recent; NaN% below cap) — this
     drops delisted/rebranded symbols whose price feed stopped (e.g. MATIC->POL).

Data sources (no credentials):
  - HL info endpoint: meta/asset-ctxs (ranking) + paginated funding history,
  - Binance klines:   hourly OHLCV (long-history price source).
See fetch.py — it reuses the patterns from research/fetch_ohlcv.py and
research/fetch_funding_history.py.

Panels (load_panel) are pd.DataFrame indexed by a regular daily DatetimeIndex,
one column per coin, columns aligned, NaN where a coin is not yet listed:
  price    — daily close (last hourly close of the day),
  fwd_ret  — next-day close-to-close return (forward, seam-safe to align),
  funding  — daily funding = SUM of the day's hourly HL funding rates
             (HL funding accrues ~hourly; the daily carry is their sum).
"""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import fetch

DATA_DIR = fetch.DATA_DIR

# ── Reproducible universe filters (tune here, not by editing a coin list) ──────
MIN_VOL_USD       = 1_000_000          # >= $1M 24h notional volume on HL
MIN_HISTORY_DAYS  = 547                # ~1.5 years of funding AND ohlcv
MAX_NAN_FRAC      = 0.02               # <= 2% NaN over a coin's own listed span
MAX_STALE_DAYS    = 5                  # last ohlcv candle must be this recent
CANDIDATE_POOL    = 60                 # how many top-by-volume coins to consider
NOW               = pd.Timestamp.now(tz="UTC")
CUTOFF            = NOW - pd.Timedelta(days=MIN_HISTORY_DAYS)
# Trim the HL first week (funding was 8h then, not 1h) — same as engine.load_data.
HISTORY_START     = pd.Timestamp("2023-06-08", tz="UTC")


def _is_bridge_token(name: str) -> bool:
    """Project policy: names ending in a digit (AVAX0/LINK0/AAVE0/kPEPE-style 1000x
    are handled separately) alias a canonical perp with independent price discovery."""
    return name[-1].isdigit()


def _coin_first_last(path: Path):
    """(first_ts, last_ts) of a cached csv, or (None, None)."""
    if not path.exists():
        return None, None
    t = pd.to_datetime(pd.read_csv(path, usecols=["time"])["time"], format="ISO8601", utc=True)
    if t.empty:
        return None, None
    return t.iloc[0], t.iloc[-1]


def _candidates() -> list[tuple[str, float]]:
    """Top liquid canonical HL perps by 24h volume, bridge tokens excluded."""
    ctxs = [c for c in fetch.hl_meta_ctxs()
            if not c["isDelisted"]
            and not _is_bridge_token(c["name"])
            and c["dayNtlVlm"] >= MIN_VOL_USD]
    ctxs.sort(key=lambda c: -c["dayNtlVlm"])
    return [(c["name"], c["dayNtlVlm"]) for c in ctxs[:CANDIDATE_POOL]]


def universe(refresh: bool = False) -> list[str]:
    """Reproducible coin list. Downloads/caches candidate data, then applies the
    history + completeness filters. Returns coins sorted alphabetically."""
    selected = []
    for coin, _vol in _candidates():
        try:
            fetch.ensure_coin(coin, refresh=refresh)
        except Exception as e:
            print(f"  {coin}: fetch failed ({type(e).__name__}: {e}); skipping")
        f_first, f_last = _coin_first_last(DATA_DIR / f"{coin}.csv")
        o_first, o_last = _coin_first_last(DATA_DIR / f"{coin}_1h.csv")
        if f_first is None or o_first is None:
            continue
        # (4) >= MIN_HISTORY_DAYS of BOTH funding and ohlcv
        if f_first > CUTOFF or o_first > CUTOFF:
            continue
        # (5a) ohlcv must be fresh (drops delisted/rebranded feeds, e.g. MATIC)
        if (NOW - o_last) > pd.Timedelta(days=MAX_STALE_DAYS):
            continue
        selected.append(coin)
    return sorted(selected)


# ── Panel loader ──────────────────────────────────────────────────────────────

def _daily_price(coin: str) -> pd.Series:
    o = pd.read_csv(DATA_DIR / f"{coin}_1h.csv")
    o["time"] = pd.to_datetime(o["time"], format="ISO8601", utc=True).dt.floor("h")
    o = o.set_index("time")["close"].sort_index()
    o = o[o.index >= HISTORY_START]
    # daily close = last hourly close of the day
    return o.resample("1D").last()


def _daily_funding(coin: str) -> pd.Series:
    f = pd.read_csv(DATA_DIR / f"{coin}.csv")
    f["time"] = pd.to_datetime(f["time"], format="ISO8601", utc=True).dt.floor("h")
    f = f.set_index("time")["fundingRate"].astype(float).sort_index()
    f = f[f.index >= HISTORY_START]
    # daily carry = SUM of the day's hourly funding rates
    return f.resample("1D").sum()


def load_panel(coins: list[str] | None = None, refresh: bool = False) -> dict:
    """Aligned daily panels for the universe.

    Returns dict with:
      coins   — list[str]
      price   — DataFrame[date x coin]  daily close
      fwd_ret — DataFrame[date x coin]  next-day close-to-close return (forward)
      funding — DataFrame[date x coin]  daily summed funding rate
    All frames share one regular daily DatetimeIndex (UTC midnight), NaN where a
    coin is not yet listed. Funding is NaN-filtered to listed span only (0-funding
    days inside a listed span are real zeros, not gaps)."""
    if coins is None:
        coins = universe(refresh=refresh)

    price_cols, fund_cols = {}, {}
    for c in coins:
        price_cols[c] = _daily_price(c)
        fund_cols[c]  = _daily_funding(c)

    price = pd.DataFrame(price_cols).sort_index()
    # Regular, gap-free daily index spanning the panel.
    full_idx = pd.date_range(price.index.min(), price.index.max(), freq="1D", tz="UTC")
    price = price.reindex(full_idx)

    funding = pd.DataFrame(fund_cols).reindex(full_idx)
    # funding only meaningful where the coin is price-listed
    funding = funding.where(price.notna())

    # forward return: r_{t+1} = P_{t+1}/P_t - 1, indexed at t (seam-safe to merge
    # with signals computed up to t). Last row is NaN by construction.
    fwd_ret = price.shift(-1) / price - 1.0

    return {"coins": coins, "price": price, "fwd_ret": fwd_ret, "funding": funding}


# ── Self-test ─────────────────────────────────────────────────────────────────

def _nan_pct_within_span(s: pd.Series) -> float:
    sv = s.dropna()
    if sv.empty:
        return 100.0
    span = s.loc[sv.index.min():sv.index.max()]
    return 100.0 * span.isna().mean()


if __name__ == "__main__":
    import sys
    refresh = "--refresh" in sys.argv

    coins = universe(refresh=refresh)
    print(f"\n=== UNIVERSE ({len(coins)} coins) ===")
    print(", ".join(coins))

    P = load_panel(coins)
    price, fwd, fund = P["price"], P["fwd_ret"], P["funding"]

    print(f"\n=== PANEL ===")
    print(f"date range : {price.index.min().date()} -> {price.index.max().date()}"
          f"  ({len(price)} days)")
    step = price.index.to_series().diff().dropna().dt.days
    print(f"index step : min={step.min()}d max={step.max()}d (regular if both ==1)")

    print(f"\n{'coin':<10}{'first':>12}{'last':>12}{'price_nan%':>12}{'fund_nan%':>12}")
    for c in coins:
        s = price[c].dropna()
        first, last = (s.index.min().date(), s.index.max().date()) if len(s) else ("-", "-")
        print(f"{c:<10}{str(first):>12}{str(last):>12}"
              f"{_nan_pct_within_span(price[c]):>12.2f}{_nan_pct_within_span(fund[c]):>12.2f}")

    print(f"\n=== SANITY ===")
    btc_last = price['BTC'].dropna().iloc[-1] if 'BTC' in price else float('nan')
    print(f"BTC last price : {btc_last:,.0f}  (plausible 10k-200k: {10_000 < btc_last < 200_000})")
    fv = fund.values[~np.isnan(fund.values)]
    pos = (fv > 0).mean() * 100
    print(f"funding sign   : {pos:.1f}% days positive, {100-pos:.1f}% negative/zero "
          f"(mean daily rate {fv.mean():.2e})")
    fr = fwd.values[~np.isnan(fwd.values)]
    print(f"fwd_ret        : mean {fr.mean():.4%}/day  std {fr.std():.4%}  "
          f"n={len(fr):,}")
    print(f"coverage       : panel cells {price.size:,}, "
          f"listed {price.notna().sum().sum():,} "
          f"({100*price.notna().sum().sum()/price.size:.1f}%)")
