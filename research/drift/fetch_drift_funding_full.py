"""
Fetch full Drift funding-rate history for a list of coins.

Pagination strategy (discovered 2026-06-03):
  - GET /market/{symbol}/fundingRates?limit=750  returns only ~570 newest records,
    meta.nextPage is always null  →  query-param pagination does NOT work.
  - GET /market/{symbol}/fundingRates/YYYY/MM/DD  returns up to 24 records per day
    and works for any historical date back to market launch.
  - Strategy: iterate day by day from today (2026-04-01 as data frozen date) backwards
    to START_DATE (2023-06-01), collecting records from each day endpoint.
    This is O(days) = ~1000 requests per coin — fast enough with 0.1s sleep.

Output: research/drift/funding_history/{COIN}.csv
Columns: ts_ms, coin, funding_rate_quote_per_base, oracle_twap,
         funding_rate_normalized, annualized_pct
"""

import csv
import datetime
import json
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://data.api.drift.trade"
HEADERS = {
    "Referer": "https://app.drift.trade",
    "Origin": "https://app.drift.trade",
    "User-Agent": "Mozilla/5.0 (research script)",
}

# Universe: coin -> Drift symbol
COINS = {
    "BTC": "BTC-PERP",
    "ETH": "ETH-PERP",
    "SOL": "SOL-PERP",
    "HYPE": "HYPE-PERP",
    "AVAX": "AVAX-PERP",
    "LINK": "LINK-PERP",
    "DOGE": "DOGE-PERP",
}

# Window
# Drift API data is frozen at ~2026-04-01 (data after this date is empty)
DATA_FROZEN_DATE = datetime.date(2026, 4, 1)
START_DATE = datetime.date(2023, 6, 1)

# HYPE launched on Drift ~2024-12-15 (tested: 2024-12-14 empty, 2024-12-15 has data)
HYPE_START_DATE = datetime.date(2024, 12, 15)

OUT_DIR = Path(__file__).parent / "funding_history"
OUT_DIR.mkdir(exist_ok=True)

FIELDNAMES = [
    "ts_ms", "coin", "funding_rate_quote_per_base",
    "oracle_twap", "funding_rate_normalized", "annualized_pct",
]


def fetch_day(symbol: str, date: datetime.date, max_retries: int = 5) -> list[dict]:
    url = f"{BASE_URL}/market/{symbol}/fundingRates/{date.year}/{date.month:02d}/{date.day:02d}"
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            return data.get("records", [])
        except urllib.error.HTTPError as e:
            if e.code == 403 or e.code == 429:
                wait = 2 ** attempt * 3  # 3, 6, 12, 24, 48 seconds
                print(f"    RATE-LIMITED {symbol} {date} (attempt {attempt+1}), sleeping {wait}s...")
                time.sleep(wait)
            else:
                print(f"    WARNING: {symbol} {date} HTTP {e.code}: {e}")
                return []
        except Exception as e:
            print(f"    WARNING: {symbol} {date} fetch error: {e}")
            return []
    print(f"    SKIP {symbol} {date}: max retries exceeded")
    return []


def process_records(coin: str, raw_records: list[dict]) -> list[dict]:
    """Convert raw API records to normalized rows."""
    rows = []
    for r in raw_records:
        try:
            ts = int(r["ts"])
            fr_raw = float(r["fundingRate"])
            oracle = float(r["oraclePriceTwap"])
            if oracle == 0:
                continue
            normalized = fr_raw / oracle          # hourly rate as decimal fraction
            annualized = normalized * 24 * 365 * 100  # annualized percent
            rows.append({
                "ts_ms": ts * 1000,
                "coin": coin,
                "funding_rate_quote_per_base": round(fr_raw, 10),
                "oracle_twap": round(oracle, 6),
                "funding_rate_normalized": round(normalized, 10),
                "annualized_pct": round(annualized, 6),
            })
        except (KeyError, ValueError, ZeroDivisionError):
            continue
    return rows


def load_existing_days(coin: str) -> set[datetime.date]:
    """Return set of dates that already have records in the saved CSV."""
    path = OUT_DIR / f"{coin}.csv"
    if not path.exists():
        return set()
    existing: set[datetime.date] = set()
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_sec = int(row["ts_ms"]) // 1000
            d = datetime.datetime.utcfromtimestamp(ts_sec).date()
            existing.add(d)
    return existing


def fetch_coin(coin: str, symbol: str, refill: bool = False) -> list[dict]:
    start = HYPE_START_DATE if coin == "HYPE" else START_DATE
    end = DATA_FROZEN_DATE

    existing_days = load_existing_days(coin) if refill else set()

    current = end
    all_rows: list[dict] = []
    seen_ts: set[int] = set()
    skipped = 0

    total_days = (end - start).days + 1
    fetched_days = 0

    print(f"  {coin}: fetching {start} → {end} ({total_days} days) ...")
    if refill:
        print(f"  {coin}: refill mode — skipping {len(existing_days)} already-covered days")

    while current >= start:
        if refill and current in existing_days:
            skipped += 1
        else:
            records = fetch_day(symbol, current)
            new_rows = process_records(coin, records)

            for row in new_rows:
                if row["ts_ms"] not in seen_ts:
                    seen_ts.add(row["ts_ms"])
                    all_rows.append(row)

            fetched_days += 1
            if fetched_days % 50 == 0:
                print(f"    {coin}: {fetched_days} new days fetched, {len(all_rows)} new records so far")

            time.sleep(0.5)  # 2 req/s — conservative to avoid CloudFront 403

        current -= datetime.timedelta(days=1)

    # Sort ascending by ts_ms
    all_rows.sort(key=lambda x: x["ts_ms"])
    print(f"  {coin}: done — {len(all_rows)} new records, {skipped} days skipped (refill)")
    return all_rows


def merge_with_existing(coin: str, new_rows: list[dict]) -> list[dict]:
    """Merge new rows with existing CSV, dedup by ts_ms, sort ascending."""
    path = OUT_DIR / f"{coin}.csv"
    existing_rows: list[dict] = []
    if path.exists():
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_rows.append({
                    "ts_ms": int(row["ts_ms"]),
                    "coin": row["coin"],
                    "funding_rate_quote_per_base": float(row["funding_rate_quote_per_base"]),
                    "oracle_twap": float(row["oracle_twap"]),
                    "funding_rate_normalized": float(row["funding_rate_normalized"]),
                    "annualized_pct": float(row["annualized_pct"]),
                })
    all_rows = existing_rows + new_rows
    seen: set[int] = set()
    merged: list[dict] = []
    for row in all_rows:
        if row["ts_ms"] not in seen:
            seen.add(row["ts_ms"])
            merged.append(row)
    merged.sort(key=lambda x: x["ts_ms"])
    return merged


def save_coin(coin: str, rows: list[dict]):
    path = OUT_DIR / f"{coin}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {coin}: saved {len(rows)} rows → {path}")


def main():
    import sys
    args = sys.argv[1:]
    refill = "--refill" in args
    target_coins = [a for a in args if not a.startswith("--")] or list(COINS.keys())

    for coin in target_coins:
        if coin not in COINS:
            print(f"Unknown coin: {coin}")
            continue
        symbol = COINS[coin]
        print(f"\n{'='*50}")
        print(f"{'Refilling' if refill else 'Fetching'} {coin} ({symbol})")
        new_rows = fetch_coin(coin, symbol, refill=refill)
        if refill and new_rows:
            merged = merge_with_existing(coin, new_rows)
            save_coin(coin, merged)
            print(f"  {coin}: merged total = {len(merged)} rows")
        elif refill:
            print(f"  {coin}: no new rows to add (all days covered)")
        elif new_rows:
            save_coin(coin, new_rows)
        else:
            print(f"  {coin}: no data!")

    print("\nDone!")


if __name__ == "__main__":
    main()
