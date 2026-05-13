"""
Скачивает funding history с Binance Futures.
Funding на Binance: каждые 8 часов (00:00, 08:00, 16:00 UTC).
"""

import requests
import pandas as pd
import time
from pathlib import Path
from datetime import datetime, timezone

API_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE",
         "ARB", "OP", "MATIC", "DOT", "ADA", "XRP", "LTC"]

START_TIME_MS = int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)

DATA_DIR = Path(__file__).parent / "data_binance"
DATA_DIR.mkdir(exist_ok=True)


def fetch_funding(symbol: str, start_ms: int):
    all_recs = []
    current = start_ms
    while True:
        resp = requests.get(API_URL, params={
            "symbol": symbol,
            "startTime": current,
            "limit": 1000,
        }, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        all_recs.extend(data)
        last_t = data[-1]["fundingTime"]
        print(f"  {symbol}: {len(all_recs)} рекордов, last={datetime.fromtimestamp(last_t/1000, tz=timezone.utc):%Y-%m-%d}")
        if len(data) < 1000:
            break
        current = last_t + 1
        time.sleep(0.2)
    return all_recs


def main():
    for coin in COINS:
        symbol = f"{coin}USDT"
        print(f"\n{symbol}...")
        try:
            recs = fetch_funding(symbol, START_TIME_MS)
            if not recs:
                print(f"  {coin}: нет данных"); continue
            df = pd.DataFrame(recs)
            df["time"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
            df["fundingRate"] = df["fundingRate"].astype(float)
            df = df[["time", "fundingRate"]].sort_values("time").reset_index(drop=True)
            out = DATA_DIR / f"{coin}.csv"
            df.to_csv(out, index=False)
            print(f"  {coin}: сохранено {len(df)} -> {out}")
        except Exception as e:
            print(f"  {coin}: ошибка {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
