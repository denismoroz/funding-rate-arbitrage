import asyncio
import httpx
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
API_URL = "https://api.hyperliquid.xyz/info"

async def fetch_hl_ohlcv(coin: str):
    async with httpx.AsyncClient() as client:
        print(f"Attempting to fetch 1h candles for {coin}...")
        try:
            end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
            start_ts = end_ts - (24 * 3600 * 1000)
            
            payload = {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": "1h",
                    "startTime": start_ts,
                    "endTime": end_ts
                }
            }
            
            resp = await client.post(API_URL, json=payload)
            resp.raise_for_status()
            candles = resp.json()
            
            if not candles:
                print(f"  No candles found for {coin}")
                return

            p = DATA_DIR / f"{coin}_1h.csv"
            import pandas as pd
            
            data = []
            for candle in candles:
                data.append({
                    "time": datetime.fromtimestamp(candle['t'] / 1000, tz=timezone.utc),
                    "close": float(candle['c']),
                    "fundingRate": 0.0
                })
            
            df = pd.DataFrame(data)
            df = df.sort_values("time")
            df.to_csv(p, index=False)
            print(f"  Successfully saved {len(df)} candles to {p}")
            
        except Exception as e:
            print(f"  Error fetching {coin}: {e}")

async def main(coins):
    tasks = [fetch_hl_ohlcv(c) for c in coins]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    coins_to_fix = ["HYPE", "PURR", "XPL"]
    asyncio.run(main(coins_to_fix))
