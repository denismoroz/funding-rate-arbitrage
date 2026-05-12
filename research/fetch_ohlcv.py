"""
Скачивает часовые свечи с Binance для списка монет.
Сохраняет в research/data/<coin>_1h.csv
"""

import requests
import pandas as pd
import time
from pathlib import Path
from datetime import datetime, timezone

BINANCE_URL = "https://api.binance.com/api/v3/klines"

COINS = [
    "BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC",
    "DOGE", "LINK", "UNI", "AAVE", "WIF", "PEPE", "TIA", "INJ",
]

START_TIME_MS = int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def fetch_ohlcv(symbol: str, start_time_ms: int) -> list:
    all_data = []
    current_start = start_time_ms
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)

    while current_start < end_time:
        resp = requests.get(BINANCE_URL, params={
            "symbol": symbol,
            "interval": "1h",
            "startTime": current_start,
            "endTime": end_time,
            "limit": 1000,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            break

        all_data.extend(data)
        current_start = data[-1][0] + 1

        last_dt = datetime.fromtimestamp(data[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  {symbol}: {len(all_data)} свечей, последняя: {last_dt}")

        time.sleep(0.1)

    return all_data


def save_ohlcv(coin: str, data: list):
    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df = df[["time", "open", "high", "low", "close", "volume"]]
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("time").reset_index(drop=True)

    path = DATA_DIR / f"{coin}_1h.csv"
    df.to_csv(path, index=False)
    print(f"  {coin}: сохранено {len(df)} свечей -> {path}")
    return df


def main():
    for coin in COINS:
        symbol = f"{coin}USDT"
        print(f"\n{coin}...")
        try:
            data = fetch_ohlcv(symbol, START_TIME_MS)
            if data:
                save_ohlcv(coin, data)
            else:
                print(f"  {coin}: нет данных")
        except Exception as e:
            print(f"  {coin}: ошибка — {e}")
        time.sleep(0.3)

    print("\nГотово!")


if __name__ == "__main__":
    main()
