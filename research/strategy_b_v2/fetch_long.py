"""Long hourly history for Strategy B limits: Binance spot 1h klines (2019-01 ..) + Binance USDT-M funding.

Binance perp funding (8h, from 2019-09) is spread evenly over the 8 hours as a proxy for HL's hourly
funding, which did not exist before 2023. Saved to the scratchpad (large), not committed.
Columns: time, close, high, fundingRate (per hour).
"""
import sys, time
from pathlib import Path
import pandas as pd, requests

OUT = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_long")
COINS = sys.argv[1:] or ["BTC", "ETH", "SOL", "AVAX", "BNB", "XRP", "LINK", "DOGE", "ADA", "LTC"]
START = int(pd.Timestamp("2019-01-01", tz="UTC").timestamp() * 1000)
END = int(pd.Timestamp("2026-09-13 12:00", tz="UTC").timestamp() * 1000)


def klines(sym):
    rows, s = [], START
    while s < END:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": sym, "interval": "1h", "startTime": s, "endTime": END, "limit": 1000}, timeout=30)
        r.raise_for_status(); d = r.json()
        if not d: break
        rows += d; s = d[-1][0] + 3_600_000; time.sleep(0.12)
    df = pd.DataFrame(rows).iloc[:, [0, 2, 4]]
    df.columns = ["t", "high", "close"]
    df["time"] = pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True)
    return df.set_index("time")[["close", "high"]].astype(float)


def funding(sym):
    rows, s = [], START
    while s < END:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": sym, "startTime": s, "endTime": END, "limit": 1000}, timeout=30)
        r.raise_for_status(); d = r.json()
        if not d: break
        rows += d; s = int(d[-1]["fundingTime"]) + 1; time.sleep(0.2)
        if len(d) < 1000: break
    f = pd.DataFrame(rows)
    f["time"] = pd.to_datetime(f["fundingTime"].astype("int64"), unit="ms", utc=True).dt.floor("h")
    return f.drop_duplicates("time").set_index("time")["fundingRate"].astype(float)


OUT.mkdir(parents=True, exist_ok=True)
for c in COINS:
    px = klines(f"{c}USDT")
    fr = funding(f"{c}USDT")
    hourly = fr.reindex(px.index, method=None)
    # each 8h settlement covers the 8 hours BEFORE it: spread it back over them
    spread = (fr / 8.0).reindex(pd.date_range(fr.index.min() - pd.Timedelta(hours=7), fr.index.max(), freq="h"))
    spread = spread.bfill(limit=7)
    px["fundingRate"] = spread.reindex(px.index).fillna(0.0)
    px.to_csv(OUT / f"{c}.csv")
    print(c, len(px), str(px.index[0])[:10], "..", str(px.index[-1])[:10], "funding from", str(fr.index.min())[:10], flush=True)
