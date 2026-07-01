"""Fetch fresh DAILY candles for the XSMOM universe from Binance (independent
long-history source), cache to scratchpad."""
import time, json, datetime as dt
from pathlib import Path
import requests

OUT = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/619a0272-724d-477f-be5a-cd442e6762db/scratchpad/daily")
OUT.mkdir(exist_ok=True)
URL = "https://api.binance.com/api/v3/klines"
KMAP = {"PEPE":"1000PEPE","BONK":"1000BONK","SHIB":"1000SHIB","FLOKI":"1000FLOKI"}
UNIVERSE = ["AAVE","ADA","APT","ARB","ATOM","AVAX","BCH","BNB","BTC","CRV","DOGE","DOT",
            "EIGEN","ENA","ETH","INJ","JTO","JUP","LINK","LTC","NEAR","PENDLE","PYTH","SOL",
            "SUI","TAO","TRX","UNI","WLD","XLM","XRP","ZRO"]
START = int(dt.datetime(2023,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)

def fetch(coin):
    sym = KMAP.get(coin,coin)+"USDT"
    rows=[]; start=START
    while True:
        r = requests.get(URL, params={"symbol":sym,"interval":"1d","startTime":start,"limit":1000}, timeout=30)
        if r.status_code!=200:
            return None if not rows else rows
        data=r.json()
        if not data: break
        rows += [(int(k[6]), float(k[4])) for k in data]  # closeTime, close
        if len(data)<1000: break
        start = data[-1][6]+1
        time.sleep(0.15)
    return rows

ok=[]; bad=[]
for c in UNIVERSE:
    rows=fetch(c)
    if rows:
        (OUT/f"{c}.csv").write_text("day_ms,close\n"+"\n".join(f"{d},{p}" for d,p in rows))
        ok.append((c,len(rows)))
    else:
        bad.append(c)
    time.sleep(0.1)
print("fetched:", ", ".join(f"{c}({n})" for c,n in ok))
print("FAILED :", bad)
