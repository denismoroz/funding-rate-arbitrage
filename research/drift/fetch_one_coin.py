"""
Fetch a single coin from Drift, saving incrementally every 50 days.
Supports --refill to skip already-covered days.

Usage:
    python3 research/drift/fetch_one_coin.py COIN [--refill]
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
SAVE_INTERVAL = 50  # save every N days fetched

OUT_DIR = Path(__file__).parent / "funding_history"
OUT_DIR.mkdir(exist_ok=True)

FIELDNAMES = [
    "ts_ms", "coin", "funding_rate_quote_per_base",
    "oracle_twap", "funding_rate_normalized", "annualized_pct",
]


def fetch_day(symbol: str, date: datetime.date, max_retries: int = 6) -> list[dict]:
    url = f"{BASE_URL}/market/{symbol}/fundingRates/{date.year}/{date.month:02d}/{date.day:02d}"
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read()).get("records", [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                wait = 3 * (2 ** attempt)
                print(f"  RATE-LIMITED {symbol} {date} attempt {attempt+1}, sleep {wait}s", flush=True)
                time.sleep(wait)
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


def load_existing(coin: str) -> tuple[list[dict], set[datetime.date]]:
    path = OUT_DIR / f"{coin}.csv"
    rows: list[dict] = []
    days: set[datetime.date] = set()
    if not path.exists():
        return rows, days
    with open(path) as f:
        for row in csv.DictReader(f):
            ts_sec = int(row["ts_ms"]) // 1000
            d = datetime.datetime.utcfromtimestamp(ts_sec).date()
            days.add(d)
            rows.append({
                "ts_ms": int(row["ts_ms"]),
                "coin": row["coin"],
                "funding_rate_quote_per_base": float(row["funding_rate_quote_per_base"]),
                "oracle_twap": float(row["oracle_twap"]),
                "funding_rate_normalized": float(row["funding_rate_normalized"]),
                "annualized_pct": float(row["annualized_pct"]),
            })
    return rows, days


def save(coin: str, all_rows: list[dict]):
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


def main():
    args = sys.argv[1:]
    refill = "--refill" in args
    coin_args = [a for a in args if not a.startswith("--")]
    if not coin_args:
        print("Usage: fetch_one_coin.py COIN [--refill]")
        sys.exit(1)
    coin = coin_args[0].upper()
    if coin not in COINS:
        print(f"Unknown coin: {coin}. Options: {list(COINS.keys())}")
        sys.exit(1)

    symbol = COINS[coin]
    start = HYPE_START_DATE if coin == "HYPE" else START_DATE
    end = DATA_FROZEN_DATE

    existing_rows, existing_days = load_existing(coin)
    print(f"{coin} ({symbol}): {len(existing_rows)} existing rows across {len(existing_days)} days", flush=True)

    missing_days = [
        d for d in (start + datetime.timedelta(n) for n in range((end - start).days + 1))
        if not (refill and d in existing_days)
    ]
    print(f"Days to fetch: {len(missing_days)}", flush=True)

    new_rows: list[dict] = []
    seen_ts = {r["ts_ms"] for r in existing_rows}

    for i, day in enumerate(missing_days):
        records = fetch_day(symbol, day)
        for row in process(coin, records):
            if row["ts_ms"] not in seen_ts:
                seen_ts.add(row["ts_ms"])
                new_rows.append(row)

        if (i + 1) % SAVE_INTERVAL == 0:
            total = save(coin, existing_rows + new_rows)
            print(f"  [{i+1}/{len(missing_days)}] saved {total} total rows (checkpoint)", flush=True)

        time.sleep(3.0)

    total = save(coin, existing_rows + new_rows)
    print(f"DONE {coin}: {total} total rows saved", flush=True)


if __name__ == "__main__":
    main()
