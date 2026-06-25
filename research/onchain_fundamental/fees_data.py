"""
DefiLlama fee data fetcher + daily panel builder for onchain_fundamental.

UNIVERSE (pre-registered in PLAN.md):

  DeFi app tokens: AAVE, UNI, JUP, ENA, JTO, CRV, LINK, EIGEN, PENDLE, ZRO
  Chain gas tokens: ETH, SOL, TRX, BNB, ARB, AVAX, SUI, INJ

AGGREGATION: sum daily fee series of ALL protocols whose name contains the
token's keyword (case-insensitive). This handles multi-version protocols
naturally (Aave V2+V3, Uniswap V2+V3+V4, etc.).

CACHE: raw JSON responses are cached in data/raw_{slug}.json to avoid
re-fetching on repeated runs. Stale if >24h old; pass refresh=True to force.

NO LOOK-AHEAD guarantee:
  fees[t] is the end-of-day realized fee for day t, as reported by DefiLlama.
  The growth signal built on top uses only fees[t'] for t' <= t. The daily
  panel aligns fee dates to date (not timestamp), so t and t+1 are calendar
  days: fees[date] is known before the NEXT day's trading session opens.

SURVIVORSHIP CAVEAT:
  All 18 coins are alive today; DefiLlama may restate historical fee data.
  This is a known bias that cannot be fully corrected; documented as a caveat.
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
DATA_DIR = _HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

LLAMA_OVERVIEW_URL = "https://api.llama.fi/overview/fees"
LLAMA_SUMMARY_URL  = "https://api.llama.fi/summary/fees/{slug}?dataType=dailyFees"
CACHE_MAX_AGE_H    = 24  # hours before cache is considered stale

# ── Universe definition (PLAN §Universe) ─────────────────────────────────────
# keyword: case-insensitive substring match on DefiLlama protocol name
# group: "defi" = DeFi app-revenue tokens, "chain" = gas-fee tokens

UNIVERSE: dict[str, dict] = {
    # DeFi group
    "AAVE":   {"keyword": "aave",       "group": "defi"},
    "UNI":    {"keyword": "uniswap",    "group": "defi"},
    "JUP":    {"keyword": "jupiter",    "group": "defi"},
    "ENA":    {"keyword": "ethena",     "group": "defi"},
    "JTO":    {"keyword": "jito",       "group": "defi"},
    "CRV":    {"keyword": "curve",      "group": "defi"},
    "LINK":   {"keyword": "chainlink",  "group": "defi"},
    "EIGEN":  {"keyword": "eigen",      "group": "defi"},
    "PENDLE": {"keyword": "pendle",     "group": "defi"},
    "ZRO":    {"keyword": "layerzero",  "group": "defi"},
    # Chain group
    "ETH":  {"keyword": "ethereum",  "group": "chain"},
    "SOL":  {"keyword": "solana",    "group": "chain"},
    "TRX":  {"keyword": "tron",      "group": "chain"},
    "BNB":  {"keyword": "bsc",       "group": "chain"},
    "ARB":  {"keyword": "arbitrum",  "group": "chain"},
    "AVAX": {"keyword": "avalanche", "group": "chain"},
    "SUI":  {"keyword": "sui",       "group": "chain"},
    "INJ":  {"keyword": "injective", "group": "chain"},
}

DEFI_COINS  = [c for c, m in UNIVERSE.items() if m["group"] == "defi"]
CHAIN_COINS = [c for c, m in UNIVERSE.items() if m["group"] == "chain"]
ALL_COINS   = list(UNIVERSE.keys())


# ── Cache utilities ───────────────────────────────────────────────────────────

def _cache_path(name: str) -> Path:
    return DATA_DIR / f"raw_{name}.json"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600
    return age_h < CACHE_MAX_AGE_H


def _fetch_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "onchain-research/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _load_or_fetch(cache_name: str, url: str, refresh: bool = False) -> dict | list:
    path = _cache_path(cache_name)
    if not refresh and _is_fresh(path):
        return json.loads(path.read_text())
    data = _fetch_json(url)
    path.write_text(json.dumps(data, separators=(",", ":")))
    return data


# ── Protocol list (overview) ─────────────────────────────────────────────────

def get_protocol_list(refresh: bool = False) -> list[dict]:
    """Fetch the full DefiLlama fees overview (2316+ protocols)."""
    data = _load_or_fetch("overview_fees", LLAMA_OVERVIEW_URL, refresh=refresh)
    if isinstance(data, dict):
        return data.get("protocols", [])
    return data  # some versions return the list directly


def slugs_for_coin(coin: str, protocols: list[dict]) -> list[str]:
    """All DefiLlama slugs matching coin keyword (case-insensitive name match)."""
    kw = UNIVERSE[coin]["keyword"].lower()
    return [p["slug"] for p in protocols if kw in p["name"].lower()]


# ── Per-slug daily fee history ────────────────────────────────────────────────

def fetch_slug_daily(slug: str, refresh: bool = False) -> pd.Series:
    """Daily fee series for one slug → pd.Series indexed by date (UTC midnight).

    Returns empty Series if the slug has no daily data.
    """
    cache_name = f"slug_{slug}"
    url = LLAMA_SUMMARY_URL.format(slug=slug)
    try:
        data = _load_or_fetch(cache_name, url, refresh=refresh)
    except Exception as e:
        print(f"    WARNING: fetch {slug} failed: {e}")
        return pd.Series(dtype=float)

    chart = data.get("totalDataChart", [])
    if not chart:
        return pd.Series(dtype=float)

    # chart = [[unix_ts, value], ...], value in USD
    ts = [pd.Timestamp(row[0], unit="s", tz="UTC").floor("D") for row in chart]
    vals = [float(row[1]) if row[1] is not None else np.nan for row in chart]
    s = pd.Series(vals, index=ts, dtype=float)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


# ── Coin-level aggregation ────────────────────────────────────────────────────

def aggregate_coin(coin: str, protocols: list[dict], refresh: bool = False) -> pd.Series:
    """Sum daily fees across all slugs matching this coin keyword.

    AGGREGATION NOTE: Different protocol versions (e.g. Aave V2 + Aave V3) are
    summed by date. Missing dates in one slug are treated as 0 (the protocol
    may not have existed yet; NaN-fill then fillna(0) before sum).
    """
    slugs = slugs_for_coin(coin, protocols)
    if not slugs:
        print(f"  {coin}: NO slugs found for keyword '{UNIVERSE[coin]['keyword']}'")
        return pd.Series(dtype=float)

    print(f"  {coin}: {len(slugs)} slugs {slugs}")
    series_list = []
    for slug in slugs:
        s = fetch_slug_daily(slug, refresh=refresh)
        if not s.empty:
            series_list.append(s)
        time.sleep(0.05)  # mild rate-limiting

    if not series_list:
        return pd.Series(dtype=float)

    # Align on union of dates, fill missing with 0 (protocol not active = 0 fees)
    # but only AFTER the protocol's first non-zero date
    df = pd.concat(series_list, axis=1)
    # For each column, backfill until first valid → NaN before first data stays NaN
    # We want: sum of what's active. NaN = not active yet OR fetch failed.
    # Policy: NaN before first data → 0 for summing; NaN in active period kept
    filled = df.copy()
    for col in filled.columns:
        first_valid = filled[col].first_valid_index()
        if first_valid is not None:
            # Before first valid: keep as NaN (not active yet)
            # From first valid onward: fill NaN with 0 (active but missing = data gap)
            filled.loc[first_valid:, col] = filled.loc[first_valid:, col].fillna(0.0)

    # Sum across protocols that are active (have at least one non-NaN series starting)
    # At each date: sum only the active series (those past their first valid index)
    total = filled.sum(axis=1, min_count=1)  # NaN if ALL are NaN at that date
    total.name = coin
    return total


# ── Full fee panel ────────────────────────────────────────────────────────────

def build_fee_panel(coins: list[str] | None = None, refresh: bool = False) -> pd.DataFrame:
    """Build daily fee panel: DataFrame[date x coin], USD.

    Columns = coins, index = daily DatetimeIndex UTC.
    NaN where coin has no fee data for that date.

    This is the RAW fee panel (USD/day). Growth signals are built on top.
    """
    if coins is None:
        coins = ALL_COINS

    print(f"\nFetching DefiLlama protocol list...")
    protocols = get_protocol_list(refresh=refresh)
    print(f"  {len(protocols)} protocols loaded")

    series: dict[str, pd.Series] = {}
    for coin in coins:
        s = aggregate_coin(coin, protocols, refresh=refresh)
        if not s.empty:
            series[coin] = s

    if not series:
        return pd.DataFrame()

    panel = pd.DataFrame(series).sort_index()
    # Regularize to daily UTC index
    full_idx = pd.date_range(panel.index.min(), panel.index.max(), freq="D", tz="UTC")
    panel = panel.reindex(full_idx)
    return panel


# ── Coverage report ───────────────────────────────────────────────────────────

def coverage_report(panel: pd.DataFrame) -> None:
    """Print coverage stats: per-coin first/last date and NaN fraction."""
    print("\n=== FEE PANEL COVERAGE ===")
    print(f"Date range: {panel.index.min().date()} -> {panel.index.max().date()}"
          f"  ({len(panel)} days)")
    print(f"\n{'Coin':<8} {'Group':<6} {'First':>12} {'Last':>12} "
          f"{'Days':>6} {'NaN%':>6} {'Mean$/d':>12}")
    for coin in panel.columns:
        s = panel[coin].dropna()
        if s.empty:
            print(f"{coin:<8} {UNIVERSE.get(coin, {}).get('group','?'):<6} "
                  f"{'NO DATA':>12}")
            continue
        nan_frac = panel[coin].isna().mean() * 100
        print(f"{coin:<8} {UNIVERSE[coin]['group']:<6} "
              f"{str(s.index.min().date()):>12} {str(s.index.max().date()):>12} "
              f"{len(s):>6} {nan_frac:>6.1f}% ${s.mean():>11,.0f}")

    # Per-date cross-section width
    valid_count = panel.notna().sum(axis=1)
    print(f"\nPer-date valid coin count:")
    print(f"  overall median: {valid_count.median():.0f}  "
          f"min: {valid_count.min():.0f}  max: {valid_count.max():.0f}")
    # Show yearly breakdown
    for yr in sorted(panel.index.year.unique()):
        mask = panel.index.year == yr
        vc = valid_count[mask]
        print(f"  {yr}: median={vc.median():.0f}  min={vc.min():.0f}  "
              f"max={vc.max():.0f}  (n={mask.sum()} days)")


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    refresh = "--refresh" in sys.argv
    coins = ALL_COINS if "--all" in sys.argv else ["ETH", "SOL", "AAVE", "UNI"]

    print(f"Building fee panel for: {coins}  (refresh={refresh})")
    panel = build_fee_panel(coins=coins, refresh=refresh)
    print(f"\nPanel shape: {panel.shape}")
    coverage_report(panel)

    # Sanity: ETH fees should be > 0 and reasonable ($1M-$100M/day)
    if "ETH" in panel.columns:
        eth = panel["ETH"].dropna()
        median_eth = eth.median()
        print(f"\nETH daily fees: median=${median_eth:,.0f} "
              f"(sanity: $1M-$50M/day)")
        assert 100_000 < median_eth < 500_000_000, \
            f"ETH median fees out of range: ${median_eth:,.0f}"

    print("\nfees_data self-test PASSED")
