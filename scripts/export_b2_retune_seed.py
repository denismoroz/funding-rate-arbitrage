"""Export the hourly history seed of the `b2_retune` book from a q-lab market snapshot.

Why: the book is replayed from 2026-03-08 02:00 UTC (the first bar of the q-lab stand's panel, so the stand and
frab start every coin's book at the same hour), but Hyperliquid's candleSnapshot serves only the last ~5000
hourly candles and the older ones cannot be fetched any more. q-lab keeps them in its snapshots. The seed holds
closes, highs and funding of the book's coins from that hour to the end of the snapshot; the engine extends it
from Hyperliquid itself (src/frab/strategy/b2/retune_history.py).

Run it with q-lab's environment (pandas + pyarrow); it imports nothing from q-lab, it only reads parquet:

    cd /Users/d/prj/q-lab && uv run --frozen python /Users/d/prj/funding-rate-arbitrage/scripts/export_b2_retune_seed.py \\
        --snapshot data/snapshots/<snapshot id> --out /Users/d/prj/funding-rate-arbitrage/deploy/b2_retune_history_seed.json

The snapshot is the one the stand's trials 72494 / 72695 of `bv2-research-headroom-retune` used.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

COINS = ("BTC", "ETH", "SOL", "AVAX")


def _col(frame: pd.DataFrame, coin: str) -> list[float | None]:
    return [None if math.isnan(v) else float(v) for v in frame[coin].to_numpy(dtype=float)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", required=True, help="q-lab snapshot directory (prices/high/funding parquet)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2026-03-08T02:00:00Z", help="first hour (open time) to export")
    args = ap.parse_args()

    snap = Path(args.snapshot)
    start = pd.Timestamp(args.start)
    prices = pd.read_parquet(snap / "prices.parquet").loc[start:]
    high = pd.read_parquet(snap / "high.parquet").reindex(prices.index)
    funding = pd.read_parquet(snap / "funding.parquet").reindex(prices.index)
    steps = prices.index.to_series().diff().dropna().unique()
    if len(steps) != 1 or steps[0] != pd.Timedelta(hours=1):
        raise SystemExit(f"the snapshot's hourly grid has holes: steps {list(steps)}")
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    seed = {
        "source": f"q-lab snapshot {snap.name}",
        "snapshot_range": [manifest["start"], manifest["end"]],
        "interval": "1h",
        "origin_ms": int(prices.index[0].timestamp() * 1000),
        "last_open_ms": int(prices.index[-1].timestamp() * 1000),
        "coins": {coin: {"close": _col(prices, coin), "high": _col(high, coin), "funding": _col(funding, coin)}
                  for coin in COINS},
    }
    Path(args.out).write_text(json.dumps(seed, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{args.out}: {len(prices)} hours {prices.index[0]} .. {prices.index[-1]}, coins {list(COINS)}")


if __name__ == "__main__":
    main()
