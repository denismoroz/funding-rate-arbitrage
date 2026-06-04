"""
Fetch a single coin from dYdX v4 indexer, saving incrementally every 500 records.
Supports --refill to re-fetch already-covered periods.

Usage:
    python3 research/dydx/fetch_one_coin.py COIN [--refill]
    python3 research/dydx/fetch_one_coin.py ALL [--refill]

dYdX v4 API:
    Base:     https://indexer.dydx.trade/v4
    Endpoint: /historicalFunding/{ticker}
    Params:   limit (max 1000), effectiveBeforeOrAt (ISO8601)
    Returns:  newest → oldest (desc)

Data availability:
    BTC/ETH/SOL/AVAX/LINK/DOGE: from ~2023-10-27
    HYPE: from ~2026-02-26

Funding interval: 1 hour
Normalization: rate (dimensionless, already normalized) * 8760 * 100 = annualized_pct
"""

import csv
import datetime
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://indexer.dydx.trade/v4"
PAGE_SIZE = 1000  # max allowed by API

# Data windows from task spec
DYDX_START = datetime.datetime(2023, 10, 27, 0, 0, 0, tzinfo=datetime.timezone.utc)
HOT_START  = datetime.datetime(2023, 6, 1,  0, 0, 0, tzinfo=datetime.timezone.utc)
COLD_END   = datetime.datetime(2026, 4, 1,  0, 0, 0, tzinfo=datetime.timezone.utc)
# HYPE listed on dYdX around 2025-02-26
HYPE_DYDX_START = datetime.datetime(2025, 2, 26, 0, 0, 0, tzinfo=datetime.timezone.utc)

COINS = ["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK", "DOGE"]

OUT_DIR = Path(__file__).parent / "funding_history"
OUT_DIR.mkdir(exist_ok=True)

FIELDNAMES = ["ts_ms", "coin", "funding_rate_raw", "funding_rate_normalized", "annualized_pct"]


def coin_start(coin: str) -> datetime.datetime:
    if coin == "HYPE":
        return HYPE_DYDX_START
    return DYDX_START


def fetch_page(ticker: str, before_or_at: datetime.datetime, max_retries: int = 6) -> list[dict]:
    ts_str = before_or_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    url = f"{BASE_URL}/historicalFunding/{ticker}?limit={PAGE_SIZE}&effectiveBeforeOrAt={ts_str}"
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "funding-arb-research/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data.get("historicalFunding", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5 * (2 ** attempt)
                print(f"  RATE-LIMITED {ticker} attempt {attempt+1}, sleep {wait}s", flush=True)
                time.sleep(wait)
            else:
                print(f"  HTTP {e.code} for {ticker} before {ts_str}", flush=True)
                return []
        except Exception as exc:
            print(f"  ERROR {ticker}: {exc}", flush=True)
            if attempt < max_retries - 1:
                time.sleep(3)
    return []


def parse_record(coin: str, r: dict):
    try:
        effective_at = r["effectiveAt"]  # e.g. "2024-01-15T03:00:00.123Z"
        dt = datetime.datetime.fromisoformat(effective_at.replace("Z", "+00:00"))
        ts_ms = int(dt.timestamp() * 1000)
        rate_raw = float(r["rate"])  # already dimensionless (e.g. 0.0001075)
        # dYdX rate is already per-interval normalized (hourly fraction of notional)
        # annualized: rate * 24h * 365d * 100%
        annualized = rate_raw * 24 * 365 * 100
        return {
            "ts_ms": ts_ms,
            "coin": coin,
            "funding_rate_raw": round(rate_raw, 10),
            "funding_rate_normalized": round(rate_raw, 10),  # already normalized
            "annualized_pct": round(annualized, 6),
        }
    except Exception:
        return None


def load_existing(coin: str):
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
                "funding_rate_raw": float(row["funding_rate_raw"]),
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
    ticker = f"{coin}-USD"
    start_dt = coin_start(coin)
    end_dt = COLD_END

    existing_rows, existing_ts = load_existing(coin)
    print(f"{coin}: {len(existing_rows)} existing rows", flush=True)

    if refill:
        # Re-fetch everything, ignore existing
        existing_rows = []
        existing_ts = set()
        print(f"  --refill: clearing existing data", flush=True)

    new_rows: list[dict] = []
    # Cursor starts at end of desired window (newest), paginating backwards
    cursor = end_dt
    total_fetched = 0
    pages = 0

    print(f"  Fetching {ticker} from {start_dt.date()} to {end_dt.date()} ...", flush=True)

    while cursor > start_dt:
        records = fetch_page(ticker, cursor)
        if not records:
            print(f"  Empty page at cursor {cursor.isoformat()}, stopping", flush=True)
            break

        pages += 1
        added_this_page = 0
        oldest_in_page = cursor

        for r in records:
            parsed = parse_record(coin, r)
            if parsed is None:
                continue
            dt = datetime.datetime.fromtimestamp(parsed["ts_ms"] / 1000, tz=datetime.timezone.utc)
            if dt < start_dt:
                continue  # before our window
            if dt >= end_dt:
                continue  # after our window (shouldn't happen with cursor)
            oldest_in_page = min(oldest_in_page, dt)
            if parsed["ts_ms"] not in existing_ts:
                existing_ts.add(parsed["ts_ms"])
                new_rows.append(parsed)
                added_this_page += 1
            total_fetched += 1

        # Move cursor to oldest record in this page minus 1 second
        oldest_dt_str = records[-1]["effectiveAt"].replace("Z", "+00:00")
        oldest_dt = datetime.datetime.fromisoformat(oldest_dt_str)
        cursor = oldest_dt - datetime.timedelta(seconds=1)

        if pages % 10 == 0:
            total = save(coin, existing_rows + new_rows)
            print(f"  [page {pages}] cursor={cursor.date()} new_rows={len(new_rows)} total={total} (checkpoint)", flush=True)

        # If we got fewer records than PAGE_SIZE, we've reached the beginning
        if len(records) < PAGE_SIZE:
            print(f"  Last page ({len(records)} records), reached beginning", flush=True)
            break

        time.sleep(0.3)  # gentle rate limiting

    total = save(coin, existing_rows + new_rows)
    print(f"DONE {coin}: {total} total rows saved ({pages} pages fetched)", flush=True)
    return total


def main():
    args = sys.argv[1:]
    refill = "--refill" in args
    coin_args = [a for a in args if not a.startswith("--")]

    if not coin_args:
        print("Usage: fetch_one_coin.py COIN [--refill]")
        print(f"  COIN: one of {COINS} or ALL")
        sys.exit(1)

    coin_input = coin_args[0].upper()
    if coin_input == "ALL":
        targets = COINS
    elif coin_input in COINS:
        targets = [coin_input]
    else:
        print(f"Unknown coin: {coin_input}. Options: {COINS} or ALL")
        sys.exit(1)

    for coin in targets:
        fetch_coin(coin, refill=refill)
        if len(targets) > 1:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
