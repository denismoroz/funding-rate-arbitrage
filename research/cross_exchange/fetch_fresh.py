"""Extend HL + Binance funding for the spread core coins from the last saved
timestamp to now. Public endpoints, no credentials. Writes to a fresh dir so the
committed research data stays untouched."""
import time, json, datetime as dt
from pathlib import Path
import requests, pandas as pd

OUT = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/spread_fresh")
(OUT / "HL").mkdir(parents=True, exist_ok=True); (OUT / "Binance").mkdir(parents=True, exist_ok=True)
COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "ARB", "OP"]
START = int(dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
NOW = int(time.time() * 1000)

def hl(coin):
    rows, s = [], START
    while s < NOW:
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "fundingHistory", "coin": coin, "startTime": s}, timeout=30)
        data = r.json()
        if not data: break
        rows += data
        last = int(data[-1]["time"])
        if last <= s or len(data) < 500: break
        s = last + 1; time.sleep(0.25)
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    return df[["coin", "fundingRate", "premium", "time"]].drop_duplicates("time")

def binance(coin):
    rows, s = [], START
    while s < NOW:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": f"{coin}USDT", "startTime": s, "limit": 1000}, timeout=30)
        if r.status_code != 200: return None
        data = r.json()
        if not data: break
        rows += data
        last = int(data[-1]["fundingTime"])
        if len(data) < 1000: break
        s = last + 1; time.sleep(0.2)
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["fundingTime"].astype("int64"), unit="ms", utc=True)
    return df[["time", "fundingRate"]].drop_duplicates("time")

rep = {}
for c in COINS:
    try:
        h = hl(c); h.to_csv(OUT / "HL" / f"{c}.csv", index=False)
    except Exception as e:
        h = None; print("HL fail", c, e)
    try:
        b = binance(c)
        if b is not None: b.to_csv(OUT / "Binance" / f"{c}.csv", index=False)
    except Exception as e:
        b = None; print("Binance fail", c, e)
    rep[c] = (None if h is None else (len(h), str(h.time.max())[:16]),
              None if b is None else (len(b), str(b.time.max())[:16]))
    print(c, rep[c])
