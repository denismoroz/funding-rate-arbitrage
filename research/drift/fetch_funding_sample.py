"""
Research throwaway: fetch last 7 days of Drift funding history for SOL, BTC, ETH.

NOTE (2026-06-03): The Drift data API appears to only have data up to ~2026-04-01.
This script pulls the most recent 7 days of AVAILABLE data (not necessarily current).

Usage:
    python3 research/drift/fetch_funding_sample.py
Output:
    research/drift/funding_sample.csv
"""
import urllib.request
import json
import csv
import datetime
from pathlib import Path

BASE_URL = "https://data.api.drift.trade"
HEADERS = {
    "Referer": "https://app.drift.trade",
    "Origin": "https://app.drift.trade",
}

def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_funding_records(symbol: str, limit: int = 750) -> list[dict]:
    url = f"{BASE_URL}/market/{symbol}/fundingRates?limit={limit}"
    data = fetch(url)
    return data.get("records", [])


def main():
    symbols = ["SOL-PERP", "BTC-PERP", "ETH-PERP"]

    # Find latest available timestamp across all coins
    all_records = {}
    for sym in symbols:
        records = get_funding_records(sym)
        all_records[sym] = records
        if records:
            latest = datetime.datetime.utcfromtimestamp(records[0]["ts"])
            print(f"{sym}: {len(records)} records, latest={latest}")

    # Use the latest timestamp found as the anchor for "7 days ago"
    latest_ts = max(
        records[0]["ts"] for records in all_records.values() if records
    )
    cutoff_ts = latest_ts - 7 * 86400
    print(
        f"\nData anchor: {datetime.datetime.utcfromtimestamp(latest_ts)} UTC"
    )
    print(
        f"7-day window starts: {datetime.datetime.utcfromtimestamp(cutoff_ts)} UTC"
    )

    rows = []
    for sym, records in all_records.items():
        coin = sym.replace("-PERP", "")
        for rec in records:
            ts = rec["ts"]
            if ts < cutoff_ts:
                continue
            fr = float(rec["fundingRate"])
            oracle = float(rec["oraclePriceTwap"])
            pct_hr = fr / oracle  # decimal per hour (e.g. -0.0000121)
            ann = pct_hr * 24 * 365 * 100
            rows.append(
                {
                    "ts_ms": ts * 1000,
                    "coin": coin,
                    "funding_rate": round(pct_hr, 10),
                    "annualized_pct": round(ann, 4),
                }
            )

    rows.sort(key=lambda x: (-x["ts_ms"], x["coin"]))

    out_path = Path(__file__).parent / "funding_sample.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["ts_ms", "coin", "funding_rate", "annualized_pct"]
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWritten {len(rows)} rows to {out_path}")
    from collections import defaultdict

    stats: dict = defaultdict(list)
    for r in rows:
        stats[r["coin"]].append(r["annualized_pct"])
    for coin, vals in sorted(stats.items()):
        avg = sum(vals) / len(vals)
        print(
            f"  {coin}: {len(vals)} records  avg_APR={avg:.2f}%  "
            f"min={min(vals):.2f}%  max={max(vals):.2f}%"
        )


if __name__ == "__main__":
    main()
