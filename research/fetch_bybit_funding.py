"""
Скачивает funding history с Bybit (USDT perpetual).
Funding на Bybit: обычно каждые 8 часов.
"""

import requests
import pandas as pd
import time
from pathlib import Path
from datetime import datetime, timezone

API_URL = "https://api.bybit.com/v5/market/funding/history"

COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE",
         "ARB", "OP", "MATIC", "DOT", "ADA", "XRP", "LTC"]

START_MS = int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_MS   = int(datetime(2026, 5, 13, tzinfo=timezone.utc).timestamp() * 1000)

DATA_DIR = Path(__file__).parent / "data_bybit"
DATA_DIR.mkdir(exist_ok=True)


def fetch_funding(symbol: str, start_ms: int, end_ms: int):
    """Bybit отдаёт по 200 записей в обратном порядке, пагинация через endTime."""
    all_recs = []
    current_end = end_ms
    while current_end > start_ms:
        resp = requests.get(API_URL, params={
            "category": "linear",
            "symbol":   symbol,
            "startTime": start_ms,
            "endTime":   current_end,
            "limit":     200,
        }, timeout=20)
        resp.raise_for_status()
        body = resp.json()
        if body.get("retCode") != 0:
            print(f"  ошибка API: {body.get('retMsg')}"); break
        data = body["result"]["list"]
        if not data:
            break
        all_recs.extend(data)
        oldest_t = int(data[-1]["fundingRateTimestamp"])
        newest_t = int(data[0]["fundingRateTimestamp"])
        print(f"  {symbol}: +{len(data)} ({datetime.fromtimestamp(oldest_t/1000, tz=timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(newest_t/1000, tz=timezone.utc):%Y-%m-%d}), всего {len(all_recs)}")
        if len(data) < 200 or oldest_t <= start_ms:
            break
        current_end = oldest_t - 1
        time.sleep(0.2)
    return all_recs


def main():
    for coin in COINS:
        symbol = f"{coin}USDT"
        print(f"\n{symbol}...")
        try:
            recs = fetch_funding(symbol, START_MS, END_MS)
            if not recs:
                print(f"  {coin}: нет данных"); continue
            df = pd.DataFrame(recs)
            df["time"] = pd.to_datetime(df["fundingRateTimestamp"].astype(int), unit="ms", utc=True)
            df["fundingRate"] = df["fundingRate"].astype(float)
            df = df[["time", "fundingRate"]].sort_values("time").drop_duplicates("time").reset_index(drop=True)
            out = DATA_DIR / f"{coin}.csv"
            df.to_csv(out, index=False)
            print(f"  {coin}: сохранено {len(df)} -> {out}")
        except Exception as e:
            print(f"  {coin}: ошибка {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
