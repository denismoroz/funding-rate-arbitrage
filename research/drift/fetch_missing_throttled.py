"""
Fetch missing Drift funding-rate data for BTC, ETH, HYPE, AVAX, LINK, DOGE.
Uses 3s sleep between requests to avoid CloudFront 403.
Resumes from the last date already present in each CSV.

Usage:
    python3 research/drift/fetch_missing_throttled.py [COIN ...]
"""

import csv
import datetime
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://data.api.drift.trade"
HEADERS = {
    "Referer": "https://app.drift.trade",
    "Origin": "https://app.drift.trade",
    "User-Agent": "Mozilla/5.0 (research script)",
}

COINS = {
    "BTC": "BTC-PERP",
    "ETH": "ETH-PERP",
    "SOL": "SOL-PERP",
    "HYPE": "HYPE-PERP",
    "AVAX": "AVAX-PERP",
    "LINK": "LINK-PERP",
    "DOGE": "DOGE-PERP",
}

DATA_FROZEN_DATE = datetime.date(2026, 4, 1)
START_DATE = datetime.date(2023, 6, 1)
HYPE_START_DATE = datetime.date(2024, 12, 15)
SAVE_INTERVAL = 30  # save every N days fetched
SLEEP_BETWEEN = 3.0  # seconds between requests

OUT_DIR = Path(__file__).parent / "funding_history"
OUT_DIR.mkdir(exist_ok=True)

FIELDNAMES = [
    "ts_ms", "coin", "funding_rate_quote_per_base",
    "oracle_twap", "funding_rate_normalized", "annualized_pct",
]


def fetch_day(symbol: str, date: datetime.date) -> list[dict]:
    """Fetch one day with backoff on 403/429. Returns [] on persistent failure."""
    url = f"{BASE_URL}/market/{symbol}/fundingRates/{date.year}/{date.month:02d}/{date.day:02d}"
    req = urllib.request.Request(url, headers=HEADERS)
    wait_times = [30, 60, 120]
    for attempt in range(len(wait_times) + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read()).get("records", [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                if attempt < len(wait_times):
                    wait = wait_times[attempt]
                    print(f"  RATE-LIMITED {symbol} {date} attempt {attempt+1}, sleeping {wait}s", flush=True)
                    time.sleep(wait)
                else:
                    print(f"  GIVING UP {symbol} {date} after 3 rate-limit backoffs", flush=True)
                    return []
            else:
                print(f"  HTTP {e.code} {symbol} {date}", flush=True)
                return []
        except Exception as exc:
            print(f"  ERROR {symbol} {date}: {exc}", flush=True)
            return []
    return []


def process(coin: str, raw: list[dict]) -> list[dict]:
    rows = []
    for r in raw:
        try:
            ts = int(r["ts"])
            fr_raw = float(r["fundingRate"])
            oracle = float(r["oraclePriceTwap"])
            if oracle == 0:
                continue
            norm = fr_raw / oracle
            ann = norm * 24 * 365 * 100
            rows.append({
                "ts_ms": ts * 1000,
                "coin": coin,
                "funding_rate_quote_per_base": round(fr_raw, 10),
                "oracle_twap": round(oracle, 6),
                "funding_rate_normalized": round(norm, 10),
                "annualized_pct": round(ann, 6),
            })
        except Exception:
            continue
    return rows


def load_existing(coin: str) -> tuple[list[dict], set[datetime.date], int]:
    """Returns (rows, covered_dates, max_ts_ms)."""
    path = OUT_DIR / f"{coin}.csv"
    rows: list[dict] = []
    days: set[datetime.date] = set()
    max_ts = 0
    if not path.exists():
        return rows, days, max_ts
    with open(path) as f:
        for row in csv.DictReader(f):
            ts_ms = int(row["ts_ms"])
            ts_sec = ts_ms // 1000
            d = datetime.datetime.utcfromtimestamp(ts_sec).date()
            days.add(d)
            if ts_ms > max_ts:
                max_ts = ts_ms
            rows.append({
                "ts_ms": ts_ms,
                "coin": row["coin"],
                "funding_rate_quote_per_base": float(row["funding_rate_quote_per_base"]),
                "oracle_twap": float(row["oracle_twap"]),
                "funding_rate_normalized": float(row["funding_rate_normalized"]),
                "annualized_pct": float(row["annualized_pct"]),
            })
    return rows, days, max_ts


def save(coin: str, all_rows: list[dict]) -> int:
    path = OUT_DIR / f"{coin}.csv"
    tmp_path = OUT_DIR / f"{coin}.csv.tmp"
    seen: set[int] = set()
    deduped = []
    for r in sorted(all_rows, key=lambda x: x["ts_ms"]):
        if r["ts_ms"] not in seen:
            seen.add(r["ts_ms"])
            deduped.append(r)
    with open(tmp_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(deduped)
    tmp_path.rename(path)
    return len(deduped)


def fetch_coin(coin: str) -> bool:
    """Fetch all missing days for coin. Returns True if completed without giving up."""
    symbol = COINS[coin]
    start = HYPE_START_DATE if coin == "HYPE" else START_DATE
    end = DATA_FROZEN_DATE

    existing_rows, existing_days, max_ts = load_existing(coin)
    print(f"\n{'='*60}", flush=True)
    print(f"{coin} ({symbol}): {len(existing_rows)} existing rows, last ts_ms={max_ts}", flush=True)

    # Determine which days need fetching
    all_days = [
        start + datetime.timedelta(n)
        for n in range((end - start).days + 1)
    ]
    missing_days = [d for d in all_days if d not in existing_days]
    print(f"  Need to fetch {len(missing_days)} days (already have {len(existing_days)})", flush=True)

    if not missing_days:
        print(f"  {coin}: already complete!", flush=True)
        return True

    new_rows: list[dict] = []
    seen_ts = {r["ts_ms"] for r in existing_rows}
    gave_up = False

    for i, day in enumerate(missing_days):
        records = fetch_day(symbol, day)
        # If fetch_day returned [] and we had rate limits, it's still [] for empty days too
        # We proceed regardless — empty days just add 0 rows
        for row in process(coin, records):
            if row["ts_ms"] not in seen_ts:
                seen_ts.add(row["ts_ms"])
                new_rows.append(row)

        # Periodic save
        if (i + 1) % SAVE_INTERVAL == 0:
            total = save(coin, existing_rows + new_rows)
            remaining = len(missing_days) - (i + 1)
            print(f"  [{i+1}/{len(missing_days)}] checkpoint: {total} total rows, ~{remaining} days left", flush=True)

        # Throttle: 3s between requests
        time.sleep(SLEEP_BETWEEN)

    total = save(coin, existing_rows + new_rows)
    print(f"DONE {coin}: {total} total rows saved", flush=True)
    return not gave_up


def main():
    args = sys.argv[1:]
    coins_to_fetch = [a.upper() for a in args if not a.startswith("--")] or [
        "HYPE", "BTC", "ETH", "AVAX", "LINK", "DOGE"
    ]

    for coin in coins_to_fetch:
        if coin == "SOL":
            print(f"SOL: skipping (already complete)", flush=True)
            continue
        if coin not in COINS:
            print(f"Unknown coin: {coin}", flush=True)
            continue
        fetch_coin(coin)

    print("\n=== All coins done ===", flush=True)


if __name__ == "__main__":
    main()
