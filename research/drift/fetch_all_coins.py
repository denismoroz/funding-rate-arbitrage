"""
Bulk fetch all 7 coins from Drift using the per-day endpoint.
Runs coins sequentially but day-by-day, with longer delays to avoid 403.

Usage:
    python3 research/drift/fetch_all_coins.py [--refill] [COIN1 COIN2 ...]

--refill: skip days already in existing CSV, only fetch missing days
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
                data = json.loads(resp.read())
            return data.get("records", [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                wait = 3 * (2 ** attempt)  # 3, 6, 12, 24, 48, 96s
                print(f"    RATE-LIMITED {symbol} {date} attempt {attempt+1}/{max_retries}, sleep {wait}s", flush=True)
                time.sleep(wait)
            else:
                print(f"    HTTP {e.code} {symbol} {date}", flush=True)
                return []
        except Exception as exc:
            print(f"    ERROR {symbol} {date}: {exc}", flush=True)
            return []
    print(f"    SKIP {symbol} {date}: all retries exhausted", flush=True)
    return []


def process_records(coin: str, raw: list[dict]) -> list[dict]:
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
        except (KeyError, ValueError, ZeroDivisionError):
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


def save_coin(coin: str, rows: list[dict]):
    path = OUT_DIR / f"{coin}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {coin}: saved {len(rows)} rows → {path}", flush=True)


def fetch_coin(coin: str, refill: bool) -> None:
    symbol = COINS[coin]
    start = HYPE_START_DATE if coin == "HYPE" else START_DATE
    end = DATA_FROZEN_DATE

    existing_rows, existing_days = load_existing(coin)

    missing_days = []
    current = start
    while current <= end:
        if not (refill and current in existing_days):
            missing_days.append(current)
        current += datetime.timedelta(days=1)

    print(f"\n{'='*55}", flush=True)
    print(f"{coin} ({symbol}): {len(missing_days)} days to fetch"
          f" ({len(existing_days)} already cached)", flush=True)

    new_rows: list[dict] = []
    seen_ts: set[int] = {r["ts_ms"] for r in existing_rows}

    for i, day in enumerate(missing_days):
        records = fetch_day(symbol, day)
        for row in process_records(coin, records):
            if row["ts_ms"] not in seen_ts:
                seen_ts.add(row["ts_ms"])
                new_rows.append(row)

        if (i + 1) % 100 == 0:
            print(f"  {coin}: {i+1}/{len(missing_days)} days, {len(new_rows)} new rows", flush=True)

        time.sleep(0.6)  # ~1.6 req/s sustained — safe under CloudFront

    all_rows = existing_rows + new_rows
    all_rows.sort(key=lambda x: x["ts_ms"])

    # Dedup
    seen2: set[int] = set()
    deduped = []
    for r in all_rows:
        if r["ts_ms"] not in seen2:
            seen2.add(r["ts_ms"])
            deduped.append(r)

    print(f"  {coin}: {len(new_rows)} new rows + {len(existing_rows)} existing = {len(deduped)} total", flush=True)
    save_coin(coin, deduped)


def main():
    args = sys.argv[1:]
    refill = "--refill" in args
    target_coins = [a for a in args if not a.startswith("--")] or list(COINS.keys())

    print(f"Mode: {'refill (skip existing days)' if refill else 'full fetch'}", flush=True)
    print(f"Coins: {target_coins}", flush=True)

    for coin in target_coins:
        if coin not in COINS:
            print(f"Unknown coin: {coin}")
            continue
        fetch_coin(coin, refill=refill)

    print("\nAll done!", flush=True)


if __name__ == "__main__":
    main()
