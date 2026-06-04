import requests
import pandas as pd
import time
import sys
from pathlib import Path
from datetime import datetime, timezone

API_URL = "https://api.hyperliquid.xyz/info"

if len(sys.argv) > 1:
    COINS = sys.argv[1:]
else:
    COINS = [
        "BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC",
        "DOGE", "LINK", "UNI", "AAVE", "WIF", "PEPE", "TIA", "INJ", "HYPE", "ZEC", "PURR", "XPL"
    ]

# Начало истории HL (примерно)
START_TIME_MS = int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def fetch_funding_history(coin: str, start_time_ms: int) -> list[dict]:
    """Скачивает всю историю funding для монеты постранично."""
    all_records = []
    current_start = start_time_ms

    while True:
        try:
            resp = requests.post(API_URL, json={
                "type": "fundingHistory",
                "coin": coin,
                "startTime": current_start,
            })
            resp.raise_for_status()
            data = resp.json()

            if not data:
                break

            all_records.extend(data)
            last_time = data[-1]["time"]

            print(f"  {coin}: получено {len(all_records)} записей, последняя: {datetime.fromtimestamp(last_time/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")

            # API возвращает не более 500 записей за раз
            if len(data) < 500:
                break

            # Следующая страница с +1ms чтобы не дублировать
            current_start = last_time + 1
            time.sleep(0.2)
        except Exception as e:
            print(f"    Error fetching data for {coin}: {e}")
            break

    return all_records


def save_coin(coin: str, records: list[dict]):
    df = pd.DataFrame(records)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=
True)
    df["fundingRate"] = df["fundingRate"].astype(float)
    df["premium"] = df["premium"].astype(float)
    # Годовые % для удобства (funding раз в час, 8760 часов в году)
    df["annualizedPct"] = df["fundingRate"] * 8760 * 100
    df = df.sort_values("time").reset_index(drop=True)

    path = DATA_DIR / f"{coin}.csv"
    df.to_csv(path, index=False)
    print(f"  {coin}: сохранено {len(df)} записей -> {path}")
    return df


def main():
    for coin in COINS:
        print(f"\n{coin}...")
        try:
            records = fetch_funding_history(coin, START_TIME_MS)
            if records:
                save_coin(coin, records)
            else:
                print(f"  {coin}: нет данных")
        except Exception as e:
            print(f"  {coin}: ошибка — {e}")
        time.sleep(0.5)

    print("\nГотово!")


if __name__ == "__main__":
    main()
