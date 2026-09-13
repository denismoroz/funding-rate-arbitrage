"""Fresh hourly HL perp candles + funding for the Strategy B coins, 2026-03-01 .. now.
Forward window for an out-of-sample check of the pre-registered STICKY24 exit.
Saved to the scratchpad, not committed."""
import time, requests, pandas as pd
from pathlib import Path
OUT = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_forward")
OUT.mkdir(parents=True, exist_ok=True)
START = int(pd.Timestamp("2026-03-01", tz="UTC").timestamp()*1000); NOW = int(time.time()*1000)
URL = "https://api.hyperliquid.xyz/info"
for coin in ("BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"):
    c = requests.post(URL, json={"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                      "startTime": START, "endTime": NOW}}, timeout=60).json()
    px = pd.DataFrame(c)
    px["time"] = pd.to_datetime(px["t"].astype("int64"), unit="ms", utc=True)
    px = px.set_index("time")[["c"]].astype(float).rename(columns={"c": "close"})
    rows, s = [], START
    while s < NOW:
        d = requests.post(URL, json={"type": "fundingHistory", "coin": coin, "startTime": s}, timeout=30).json()
        if not d: break
        rows += d; last = int(d[-1]["time"])
        if len(d) < 500 or last <= s: break
        s = last + 1; time.sleep(0.25)
    f = pd.DataFrame(rows); f["time"] = pd.to_datetime(f["time"].astype("int64"), unit="ms", utc=True).dt.floor("h")
    f = f.drop_duplicates("time").set_index("time")["fundingRate"].astype(float)
    df = px.join(f, how="left"); df["fundingRate"] = df["fundingRate"].fillna(0.0)
    df.to_csv(OUT / f"{coin}.csv")
    print(coin, len(df), str(df.index[0])[:16], "..", str(df.index[-1])[:16])
