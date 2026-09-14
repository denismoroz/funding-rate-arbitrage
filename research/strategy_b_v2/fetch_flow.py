"""Hourly Binance spot klines WITH taker-buy volume for BTC/ETH/SOL/AVAX (order-flow idea for B's hedge entry).
Saved to the scratchpad (large), not committed. Columns: time, volume, taker_buy_volume."""
import time
from pathlib import Path
import pandas as pd, requests

OUT = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_flow")
START = int(pd.Timestamp("2019-01-01", tz="UTC").timestamp() * 1000)
END = int(pd.Timestamp("2026-09-13 12:00", tz="UTC").timestamp() * 1000)
OUT.mkdir(parents=True, exist_ok=True)
for c in ("BTC", "ETH", "SOL", "AVAX"):
    rows, s = [], START
    while s < END:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": f"{c}USDT", "interval": "1h", "startTime": s, "endTime": END, "limit": 1000}, timeout=30)
        r.raise_for_status(); d = r.json()
        if not d: break
        rows += d; s = d[-1][0] + 3_600_000; time.sleep(0.1)
    df = pd.DataFrame([(x[0], float(x[5]), float(x[9])) for x in rows], columns=["t", "volume", "taker_buy_volume"])
    df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df.set_index("time")[["volume", "taker_buy_volume"]].to_csv(OUT / f"{c}.csv")
    print(c, len(df), flush=True)
