"""
Fetch HL funding history for the Drift comparison universe.
Copies from research/data/ (already cached) into research/drift/funding_history_hl/
with the same schema used for Drift: ts_ms, coin, funding_rate_normalized, annualized_pct.

HL fundingRate is already a normalized hourly decimal fraction — no further division needed.
HL annualizedPct = fundingRate * 24 * 365 * 100

Only copies data up to 2026-04-01 to match the Drift data freeze date.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = Path(__file__).parent / "funding_history_hl"
OUT_DIR.mkdir(exist_ok=True)

# Match the Drift data window
DRIFT_DATA_END = datetime(2026, 4, 1, 23, 59, 59, tzinfo=timezone.utc)

COINS = ["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK", "DOGE"]

FIELDNAMES = ["ts_ms", "coin", "funding_rate_normalized", "annualized_pct"]

HL_FUNDING_PRECISION = 10


def copy_coin(coin: str):
    src = SRC_DIR / f"{coin}.csv"
    if not src.exists():
        print(f"  {coin}: source not found at {src}")
        return

    out_path = OUT_DIR / f"{coin}.csv"
    rows = []

    with open(src) as f:
        reader = csv.DictReader(f)
        for row in reader:
            # HL time column is ISO datetime string (e.g. "2023-06-01 00:00:00.139000+00:00")
            try:
                dt = datetime.fromisoformat(row["time"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue

            if dt > DRIFT_DATA_END:
                continue

            ts_ms = int(dt.timestamp() * 1000)
            fr = float(row["fundingRate"])
            ann = fr * 24 * 365 * 100

            rows.append({
                "ts_ms": ts_ms,
                "coin": coin,
                "funding_rate_normalized": round(fr, HL_FUNDING_PRECISION),
                "annualized_pct": round(ann, 6),
            })

    rows.sort(key=lambda x: x["ts_ms"])

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    if rows:
        first_dt = datetime.utcfromtimestamp(rows[0]["ts_ms"] / 1000).date()
        last_dt = datetime.utcfromtimestamp(rows[-1]["ts_ms"] / 1000).date()
        print(f"  {coin}: {len(rows)} rows, {first_dt} → {last_dt} → {out_path}")
    else:
        print(f"  {coin}: 0 rows written")


def main():
    print(f"Copying HL funding data (up to {DRIFT_DATA_END.date()}) to {OUT_DIR}")
    for coin in COINS:
        copy_coin(coin)
    print("Done!")


if __name__ == "__main__":
    main()
