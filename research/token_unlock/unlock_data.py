"""
Token-unlock data layer: fetch + cache DeFiLlama emission JSON, parse cliff events.

API (free, no auth needed):
  - Protocol list: https://defillama-datasets.llama.fi/emissionsProtocolsList
  - Per-protocol:  https://defillama-datasets.llama.fi/emissions/{slug}

DO NOT use api.llama.fi/emissions (paid, 402).

Cliff event schema:
  timestamp    : unix seconds (sometimes ms if > 1e12 → divide by 1000)
  noOfTokens   : list[float] → sum for total tokens
  unlockType   : "cliff" | "linear" | ...
  category     : string (informational)
  size         : sum(noOfTokens) / maxSupply  (fraction of max supply)

Usage:
  from unlock_data import load_events
  events = load_events()   # list of dicts: date, coin, slug, tokens, size, category
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = Path(__file__).parent
DATA_DIR = _HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

PROTOCOLS_CACHE = DATA_DIR / "_protocols_list.json"
PROTOCOLS_URL = "https://defillama-datasets.llama.fi/emissionsProtocolsList"
EMISSIONS_URL = "https://defillama-datasets.llama.fi/emissions/{slug}"

# Cache TTL in seconds (1 day)
CACHE_TTL = 86_400

# Coin → DeFiLlama slug mapping (seed + extended)
COIN_SLUG: dict[str, str] = {
    # seed from data/coin_slug_seed.json
    "AAVE": "aave",
    "UNI": "uniswap",
    "CRV": "curve-finance",
    "ARB": "arbitrum",
    "ENA": "ethena",
    "JTO": "jito",
    "JUP": "jupiter",
    "SEI": "sei",
    "APT": "aptos",
    "ONDO": "ondo-finance",
    "PENDLE": "pendle",
    "WLD": "worldcoin",
    "TIA": "celestia",
    "TON": "ton",
    "NEAR": "near",
    "DYDX": "dydx",
    "ZRO": "layerzero",
    "MAV": "maverick-protocol",
    "STG": "stargate-finance",
    "PENGU": "pudgy-penguins",
    "TRUMP": "official-trump",
    "VIRTUAL": "virtuals-protocol",
    "OMNI": "omni-network",
    "MORPHO": "morpho",
    # extended: coins with local _1h.csv data + slug in protocol list
    "SOL": "solana",
    "ETH": "ethereum",
    "BTC": "bitcoin",
    "AVAX": "avalanche",
    "LINK": "chainlink",
    "OM": "mantra-dao",
    "TAO": "bittensor",
    "LTC": "litecoin",
    "SUI": "sui-foundation",
    "INJ": "injective-orderbook",
    "PYTH": "pyth",
    "RDNT": "radiant",
}


def _cache_path(slug: str) -> Path:
    return DATA_DIR / f"emissions_{slug}.json"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL


def _fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "research/unlock_data 1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_protocols_list() -> list[str]:
    """Return list of available slugs. Cached 1 day."""
    if _is_fresh(PROTOCOLS_CACHE):
        return json.loads(PROTOCOLS_CACHE.read_bytes())
    data = _fetch_url(PROTOCOLS_URL)
    PROTOCOLS_CACHE.write_bytes(data)
    return json.loads(data)


def _fetch_emissions(slug: str, force: bool = False) -> Optional[dict]:
    """Fetch emission JSON for a slug, caching to disk."""
    path = _cache_path(slug)
    if not force and _is_fresh(path):
        return json.loads(path.read_bytes())
    url = EMISSIONS_URL.format(slug=slug)
    try:
        raw = _fetch_url(url, timeout=30)
        path.write_bytes(raw)
        return json.loads(raw)
    except Exception as e:
        print(f"  [unlock_data] {slug}: fetch failed — {e}")
        # return stale cache if available
        if path.exists():
            return json.loads(path.read_bytes())
        return None


def _parse_timestamp(ts) -> Optional[int]:
    """Coerce timestamp to unix seconds (int). Returns None if unparseable."""
    try:
        t = int(float(str(ts)))
    except (ValueError, TypeError):
        return None
    if t > 1e12:       # milliseconds → seconds
        t = t // 1000
    if t < 0 or t > 4_000_000_000:
        return None
    return t


def _parse_cliff_events(data: dict, coin: str, slug: str) -> list[dict]:
    """Extract cliff unlock events from emission JSON.

    Returns list of dicts:
      coin, slug, date (pd.Timestamp UTC), tokens (float), size (fraction),
      category (str), timestamp_unix (int)
    """
    events = data.get("metadata", {}).get("events", [])
    max_supply = float(data.get("supplyMetrics", {}).get("maxSupply") or 0)
    out = []
    for ev in events:
        if ev.get("unlockType") != "cliff":
            continue
        ts = _parse_timestamp(ev.get("timestamp"))
        if ts is None:
            continue
        raw_tokens = ev.get("noOfTokens", [])
        if not isinstance(raw_tokens, list):
            raw_tokens = [raw_tokens]
        try:
            tokens = float(sum(float(x) for x in raw_tokens if x is not None))
        except (TypeError, ValueError):
            continue
        if tokens <= 0:
            continue
        size = (tokens / max_supply) if max_supply > 0 else float("nan")
        date = pd.Timestamp(datetime.fromtimestamp(ts, tz=timezone.utc)).normalize()
        out.append({
            "coin": coin,
            "slug": slug,
            "date": date,
            "timestamp_unix": ts,
            "tokens": tokens,
            "max_supply": max_supply,
            "size": size,
            "category": str(ev.get("category", "")),
        })
    return out


def load_events(
    coins: Optional[list[str]] = None,
    force_refresh: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """Fetch and parse cliff unlock events for all mapped coins.

    Parameters
    ----------
    coins : subset of COIN_SLUG keys to load (default: all).
    force_refresh : re-fetch from network even if cache is fresh.
    verbose : print progress.

    Returns
    -------
    DataFrame with columns:
        coin, slug, date, timestamp_unix, tokens, max_supply, size, category
    sorted by date.
    """
    mapping = {c: s for c, s in COIN_SLUG.items()
               if coins is None or c in coins}
    all_events: list[dict] = []
    for coin, slug in mapping.items():
        if verbose:
            print(f"  Loading {coin} ({slug})...", end=" ")
        data = _fetch_emissions(slug, force=force_refresh)
        if data is None:
            if verbose:
                print("SKIP (no data)")
            continue
        evs = _parse_cliff_events(data, coin, slug)
        if verbose:
            print(f"{len(evs)} cliff events")
        all_events.extend(evs)

    if not all_events:
        return pd.DataFrame(columns=["coin", "slug", "date", "timestamp_unix",
                                     "tokens", "max_supply", "size", "category"])
    df = pd.DataFrame(all_events)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values(["date", "coin"]).reset_index(drop=True)
    return df


def summarize_events(df: pd.DataFrame, size_threshold: float = 0.01) -> pd.DataFrame:
    """Print summary of cliff events by coin with threshold filter."""
    big = df[df["size"] >= size_threshold].copy()
    print(f"\nCliff events summary (size >= {size_threshold:.1%}):")
    print(f"  Total events: {len(df)}, above threshold: {len(big)}")
    print(f"  Coins: {df['coin'].nunique()} total, "
          f"{big['coin'].nunique()} with large cliffs")
    grp = big.groupby("coin").agg(
        n_events=("date", "count"),
        first=("date", "min"),
        last=("date", "max"),
        max_size=("size", "max"),
    ).sort_values("max_size", ascending=False)
    print(grp.to_string())
    return big


if __name__ == "__main__":
    print("=== Token-Unlock Data Layer ===")
    print("Fetching cliff events...")
    df = load_events(verbose=True)
    print(f"\nTotal cliff events: {len(df)}")
    print(f"Coins: {sorted(df['coin'].unique())}")
    print(f"Date range: {df['date'].min().date()} → {df['date'].max().date()}")

    summarize_events(df, size_threshold=0.01)
    print("\nSample (large events):")
    print(df[df["size"] >= 0.02].nlargest(10, "size")[
        ["coin", "date", "size", "category"]].to_string())
