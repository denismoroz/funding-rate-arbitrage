"""
Скачивает funding history с Drift Protocol (Solana).

Источник: drift-historical-data-v2 S3 bucket (per-day gzipped CSV).
Формат сырых данных:
  fundingRate — $ per base unit per hour (premium markTWAP-oracleTWAP / 24 in some scaling)
  → rate fraction per hour = fundingRate / oraclePriceTwap

Funding на Drift: каждый час (как HL).
"""

import requests
import io
import pandas as pd
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROGRAM_ID = "dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH"
S3_BASE = f"https://drift-historical-data-v2.s3.eu-west-1.amazonaws.com/program/{PROGRAM_ID}/market"

# Берём те же монеты что и в основном анализе. На Drift есть BTC/ETH/SOL/AVAX/LINK/AAVE/DOGE perps.
MARKETS = ["BTC-PERP", "ETH-PERP", "SOL-PERP", "AVAX-PERP", "LINK-PERP",
           "AAVE-PERP", "DOGE-PERP", "ARB-PERP", "OP-PERP", "MATIC-PERP"]

# Drift v2 запустился в ноябре 2022 для BTC/ETH/SOL.
# Берём с 2023-06 для согласованности с HL анализом.
START_DATE = datetime(2023, 6, 1, tzinfo=timezone.utc).date()
END_DATE   = datetime(2026, 5, 13, tzinfo=timezone.utc).date()

DATA_DIR = Path(__file__).parent / "data_drift"
DATA_DIR.mkdir(exist_ok=True)


def fetch_day(market: str, day_str: str):
    """Возвращает DataFrame с funding records за день, или None если файл отсутствует."""
    url = f"{S3_BASE}/{market}/fundingRateRecords/{day_str[:4]}/{day_str}"
    r = requests.get(url, timeout=20)
    if r.status_code != 200 or len(r.content) < 100:
        return None
    try:
        df = pd.read_csv(io.StringIO(r.text))
    except Exception:
        return None
    return df


def fetch_market(market: str):
    """Загружает весь период для одной монеты."""
    all_dfs = []
    cur = START_DATE
    missing_streak = 0
    while cur <= END_DATE:
        day_str = cur.strftime("%Y%m%d")
        df = fetch_day(market, day_str)
        if df is not None and len(df) > 0:
            all_dfs.append(df)
            missing_streak = 0
        else:
            missing_streak += 1
            # Если 30 дней подряд нет — монета не торговалась
            if missing_streak >= 30 and not all_dfs:
                pass  # просто продолжаем искать начало
        if cur.day == 1:
            print(f"    {market}: до {cur} собрано {len(all_dfs)} дней")
        cur += timedelta(days=1)
    if not all_dfs:
        return None
    big = pd.concat(all_dfs, ignore_index=True)
    big = big.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return big


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Преобразует в наш стандартный формат: time, fundingRate (fraction per hour)."""
    df = df.copy()
    df["time"] = pd.to_datetime(df["ts"].astype(int), unit="s", utc=True)
    # rate per hour = (mark - oracle) / oracle / 24 — но в стораджне хранится уже /24
    # fundingRate в data = ($mark_twap - $oracle_twap) / 24
    # Доля за час = fundingRate / oraclePriceTwap
    df["fundingRate"] = df["fundingRate"] / df["oraclePriceTwap"]
    return df[["time", "fundingRate"]].sort_values("time").reset_index(drop=True)


def main():
    for market in MARKETS:
        coin = market.replace("-PERP", "")
        print(f"\n{market}...")
        out_path = DATA_DIR / f"{coin}.csv"
        if out_path.exists():
            print(f"  {coin}: уже есть, пропускаю")
            continue
        try:
            raw = fetch_market(market)
            if raw is None or len(raw) == 0:
                print(f"  {coin}: нет данных")
                continue
            df = normalize(raw)
            df.to_csv(out_path, index=False)
            print(f"  {coin}: сохранено {len(df)} записей за {(df['time'].max()-df['time'].min()).days} дней -> {out_path}")
            # Среднее annualized
            avg_annual = df["fundingRate"].mean() * 8760 * 100
            print(f"  {coin}: средний funding rate annualized = {avg_annual:.2f}%")
        except Exception as e:
            print(f"  {coin}: ошибка {e}")
        time.sleep(0.3)


if __name__ == "__main__":
    main()
