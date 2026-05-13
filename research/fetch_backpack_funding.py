"""
Скачивает funding history с Backpack Exchange.
Funding на Backpack: каждый час (3600000ms интервал).
История: с 2025-01 для BTC/ETH/SOL.
"""

import requests
import pandas as pd
import time
from pathlib import Path

API = "https://api.backpack.exchange/api/v1/fundingRates"

# Берём те же монеты что и в основном анализе.
COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE",
         "ARB", "OP", "MATIC", "DOT", "ADA", "XRP", "LTC"]

DATA_DIR = Path(__file__).parent / "data_backpack"
DATA_DIR.mkdir(exist_ok=True)


def fetch_market(symbol: str):
    """Пагинируем по offset до пустого ответа."""
    all_recs = []
    offset = 0
    while True:
        r = requests.get(API, params={
            "symbol": symbol,
            "limit": 1000,
            "offset": offset,
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_recs.extend(data)
        if len(data) < 1000:
            break
        offset += 1000
        time.sleep(0.2)
        if offset % 2000 == 0:
            print(f"  {symbol}: offset={offset}, {len(all_recs)} records, oldest={data[-1]['intervalEndTimestamp']}")
    return all_recs


def main():
    for coin in COINS:
        symbol = f"{coin}_USDC_PERP"
        print(f"\n{symbol}...")
        out = DATA_DIR / f"{coin}.csv"
        if out.exists():
            print(f"  {coin}: уже есть, пропускаю")
            continue
        try:
            recs = fetch_market(symbol)
            if not recs:
                print(f"  {coin}: нет данных"); continue
            df = pd.DataFrame(recs)
            df["time"] = pd.to_datetime(df["intervalEndTimestamp"], utc=True)
            df["fundingRate"] = df["fundingRate"].astype(float)
            df = df[["time", "fundingRate"]].sort_values("time").drop_duplicates("time").reset_index(drop=True)
            df.to_csv(out, index=False)
            avg_annual = df["fundingRate"].mean() * 8760 * 100
            days = (df["time"].max() - df["time"].min()).days
            print(f"  {coin}: сохранено {len(df)} записей за {days} дней, avg annualized = {avg_annual:.2f}%")
        except Exception as e:
            print(f"  {coin}: ошибка {e}")
        time.sleep(0.4)


if __name__ == "__main__":
    main()
