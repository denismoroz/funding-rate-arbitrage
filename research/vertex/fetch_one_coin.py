"""
Fetch a single coin from Vertex Protocol archive indexer, saving incrementally.
Supports --refill to re-fetch already-covered periods.

Usage:
    python3 research/vertex/fetch_one_coin.py COIN [--refill]
    python3 research/vertex/fetch_one_coin.py ALL [--refill]

Vertex Archive API:
    Base:     https://archive.prod.vertexprotocol.com/v1
    Endpoint: POST /query with {"type": "market_snapshots", "interval": {...}, "product_ids": [...]}
    Returns:  list of snapshots with funding_rates dict (product_id -> rate_x18)

    funding_rate_x18 is an int64 encoded as string, representing rate * 10^18
    Example: "12345678901234567" -> rate = 0.0000123456... per hour
    annualized_pct = (rate_x18 / 1e18) * 24 * 365 * 100

Data availability:
    Vertex Protocol SHUT DOWN ~July 17-19, 2025 (TVL $58M -> $0 in 2 days)
    Hot window (2023-06-01 -> 2024-12-31): data available IF API accessible
    Cold window (2025-01-01 -> 2026-04-01): data available ONLY until ~2025-07-17

IMPORTANT: This script requires access to archive.prod.vertexprotocol.com which is
geo-blocked from DigitalOcean Frankfurt IPs via Vercel SNI-based infrastructure.
If running from a different IP (e.g., local machine or US/EU non-blocked IP), this
script should work correctly.

Product IDs (Arbitrum mainnet):
    BTC  -> perp product_id = 2
    ETH  -> perp product_id = 4
    SOL  -> perp product_id = unknown (not confirmed, estimated ~12 or higher)
    AVAX -> perp product_id = unknown
    LINK -> perp product_id = unknown
    DOGE -> perp product_id = unknown
    HYPE -> NOT LISTED on Vertex

Funding interval: 1 hour (granularity=3600 in market_snapshots)
"""

import csv
import datetime
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://archive.prod.vertexprotocol.com/v1"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (research script)",
}

# Product IDs for perp markets (Arbitrum mainnet)
# BTC and ETH are confirmed from SDK tests; others are estimated
COINS = {
    "BTC": 2,
    "ETH": 4,
    # SOL, AVAX, LINK, DOGE: product_ids unconfirmed without live API access
    # Once API is accessible, call get_all_products to discover correct IDs
}

# Universe coins with known status
UNIVERSE_COINS = ["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK", "DOGE"]
HYPE_NOT_ON_VERTEX = True  # HYPE is HL-native, not listed on Vertex

HOT_START = datetime.datetime(2023, 6, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
COLD_END = datetime.datetime(2026, 4, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
# Vertex shutdown: TVL dropped to $0 on July 19, 2025
VERTEX_SHUTDOWN_DATE = datetime.datetime(2025, 7, 19, 0, 0, 0, tzinfo=datetime.timezone.utc)

# Snapshots per request (market_snapshots API count parameter)
PAGE_SIZE = 500
# granularity in seconds (3600 = 1 hour)
GRANULARITY = 3600

OUT_DIR = Path(__file__).parent / "funding_history"
OUT_DIR.mkdir(exist_ok=True)

FIELDNAMES = ["ts_ms", "coin", "funding_rate_raw", "funding_rate_normalized", "annualized_pct"]


def fetch_page(product_id: int, max_ts: int, max_retries: int = 6) -> list[dict]:
    """
    Fetch market snapshots from archive indexer.

    market_snapshots API returns snapshots descending in time.
    The 'interval' parameter specifies count (number of snapshots) and granularity (seconds).
    There's no direct 'before' cursor; we use max_ts in the interval.

    NOTE: The actual API uses:
    {
      "type": "market_snapshots",
      "interval": {"count": N, "granularity": 3600, "max_time": unix_ts},
      "product_ids": [product_id]
    }
    Some versions use just "count" and "granularity" and return the most recent N snapshots.
    For pagination, we use max_time to walk backwards.
    """
    payload = {
        "type": "market_snapshots",
        "interval": {
            "count": PAGE_SIZE,
            "granularity": GRANULARITY,
            "max_time": max_ts,
        },
        "product_ids": [product_id],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE_URL + "/query", data=data, headers=HEADERS)

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                return result.get("snapshots", [])
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = 5 * (2 ** attempt)
                print(f"  RATE-LIMITED product_id={product_id} attempt {attempt+1}, sleep {wait}s", flush=True)
                time.sleep(wait)
            else:
                print(f"  HTTP {e.code} product_id={product_id} max_ts={max_ts}", flush=True)
                return []
        except Exception as exc:
            print(f"  ERROR product_id={product_id} max_ts={max_ts}: {exc}", flush=True)
            if attempt < max_retries - 1:
                time.sleep(3)
    return []


def parse_snapshot(coin: str, product_id: int, snapshot: dict) -> dict | None:
    """
    Parse a single market snapshot to extract funding rate for the product.

    funding_rates is a dict: str(product_id) -> funding_rate_x18 string
    funding_rate_x18 = rate_fraction * 1e18 (int64 encoded as string)
    """
    try:
        ts = int(snapshot["timestamp"])
        funding_rates = snapshot.get("funding_rates", {})
        pid_str = str(product_id)
        if pid_str not in funding_rates:
            return None
        raw_x18 = int(funding_rates[pid_str])
        rate_fraction = raw_x18 / 1e18  # normalized fraction per hour
        annualized = rate_fraction * 24 * 365 * 100  # annualized %
        return {
            "ts_ms": ts * 1000,
            "coin": coin,
            "funding_rate_raw": raw_x18,
            "funding_rate_normalized": round(rate_fraction, 12),
            "annualized_pct": round(annualized, 6),
        }
    except Exception:
        return None


def load_existing(coin: str) -> tuple[list[dict], set[int]]:
    path = OUT_DIR / f"{coin}.csv"
    rows: list[dict] = []
    ts_set: set[int] = set()
    if not path.exists():
        return rows, ts_set
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = int(row["ts_ms"])
            ts_set.add(ts)
            rows.append({
                "ts_ms": ts,
                "coin": row["coin"],
                "funding_rate_raw": int(float(row["funding_rate_raw"])),
                "funding_rate_normalized": float(row["funding_rate_normalized"]),
                "annualized_pct": float(row["annualized_pct"]),
            })
    return rows, ts_set


def save(coin: str, all_rows: list[dict]) -> int:
    path = OUT_DIR / f"{coin}.csv"
    seen: set[int] = set()
    deduped = []
    for r in sorted(all_rows, key=lambda x: x["ts_ms"]):
        if r["ts_ms"] not in seen:
            seen.add(r["ts_ms"])
            deduped.append(r)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(deduped)
    return len(deduped)


def fetch_coin(coin: str, refill: bool = False):
    if coin not in COINS:
        print(f"  SKIP {coin}: product_id unknown (API access needed to discover)")
        return 0
    if coin == "HYPE":
        print(f"  SKIP {coin}: HYPE is not listed on Vertex Protocol")
        return 0

    product_id = COINS[coin]
    start_dt = HOT_START
    # Cap end at Vertex shutdown date (data unavailable after July 2025)
    end_dt = min(COLD_END, VERTEX_SHUTDOWN_DATE)

    existing_rows, existing_ts = load_existing(coin)
    print(f"{coin} (product_id={product_id}): {len(existing_rows)} existing rows", flush=True)

    if refill:
        existing_rows = []
        existing_ts = set()
        print(f"  --refill: clearing existing data", flush=True)

    new_rows: list[dict] = []
    # Start from end, paginate backwards (market_snapshots returns newest first)
    cursor_ts = int(end_dt.timestamp())
    start_ts = int(start_dt.timestamp())
    total_fetched = 0
    pages = 0

    print(f"  Fetching {coin} from {start_dt.date()} to {end_dt.date()} ...", flush=True)
    print(f"  Note: Vertex shutdown ~2025-07-19, cold window data incomplete", flush=True)

    while cursor_ts > start_ts:
        snapshots = fetch_page(product_id, cursor_ts)
        if not snapshots:
            print(f"  Empty page at cursor {cursor_ts}, stopping", flush=True)
            break

        pages += 1
        oldest_in_page = cursor_ts

        for snap in snapshots:
            parsed = parse_snapshot(coin, product_id, snap)
            if parsed is None:
                continue
            snap_ts = parsed["ts_ms"] // 1000
            if snap_ts < start_ts or snap_ts >= int(end_dt.timestamp()):
                continue
            oldest_in_page = min(oldest_in_page, snap_ts)
            if parsed["ts_ms"] not in existing_ts:
                existing_ts.add(parsed["ts_ms"])
                new_rows.append(parsed)
            total_fetched += 1

        # Move cursor to oldest snapshot in page
        oldest_snap_ts = min(int(s["timestamp"]) for s in snapshots)
        cursor_ts = oldest_snap_ts - GRANULARITY  # step back one hour

        if pages % 10 == 0:
            total = save(coin, existing_rows + new_rows)
            print(f"  [page {pages}] cursor={datetime.datetime.utcfromtimestamp(cursor_ts).date()} "
                  f"new_rows={len(new_rows)} total={total} (checkpoint)", flush=True)

        if len(snapshots) < PAGE_SIZE:
            print(f"  Last page ({len(snapshots)} snapshots), reached beginning", flush=True)
            break

        time.sleep(0.5)  # gentle rate limiting

    total = save(coin, existing_rows + new_rows)
    print(f"DONE {coin}: {total} total rows saved ({pages} pages fetched)", flush=True)
    return total


def main():
    args = sys.argv[1:]
    refill = "--refill" in args
    coin_args = [a for a in args if not a.startswith("--")]

    if not coin_args:
        print("Usage: fetch_one_coin.py COIN [--refill]")
        print(f"  COIN: one of {UNIVERSE_COINS} or ALL")
        print()
        print("NOTE: Vertex Protocol shut down ~July 2025. API may not be accessible.")
        print("      Only BTC (product_id=2) and ETH (product_id=4) have confirmed product IDs.")
        sys.exit(1)

    coin_input = coin_args[0].upper()
    if coin_input == "ALL":
        targets = [c for c in UNIVERSE_COINS if c != "HYPE" and c in COINS]
    elif coin_input in UNIVERSE_COINS:
        targets = [coin_input]
    else:
        print(f"Unknown coin: {coin_input}. Options: {UNIVERSE_COINS} or ALL")
        sys.exit(1)

    for coin in targets:
        fetch_coin(coin, refill=refill)
        if len(targets) > 1:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
