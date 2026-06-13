"""
bear_fetch.py — Binance USDⓈ-M futures data fetcher for the 2021-2023 bear-regime
stress test.

Fetches:
  - Binance hourly OHLCV (klines) for a basket of liquid perps that traded through
    2021-01 → 2023-01.
  - Binance USDⓈ-M futures funding rates (every 8h) for the same basket.

Cache convention:
  data/bear_<COIN>_1h.csv  — hourly OHLCV
  data/bear_<COIN>_funding.csv — raw Binance funding (every 8h)

NEVER overwrites the HL-era files (no bear_ prefix on those files).

Bridge-token policy: any coin name ending in a digit is excluded.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

BINANCE_KLINES  = "https://api.binance.com/api/v3/klines"
BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"

BEAR_START_MS = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
BEAR_END_MS   = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

# Binance 1000x naming map (same as fetch.py)
_K_MAP = {"PEPE": "1000PEPE", "BONK": "1000BONK", "SHIB": "1000SHIB", "FLOKI": "1000FLOKI"}

# Candidate basket: ~20 large-cap perps that traded actively on Binance 2021-2023.
# Bridge tokens (name ending in digit) are excluded per project policy.
# MATIC listed mid-2020, is included; POL is the rebrand and NOT listed here.
BEAR_BASKET = [
    "BTC", "ETH", "BNB", "XRP", "ADA", "SOL", "DOGE", "DOT", "LTC", "LINK",
    "BCH", "AVAX", "MATIC", "ATOM", "UNI", "TRX", "ETC", "FIL", "NEAR", "AAVE",
]


def binance_symbol(coin: str) -> str:
    """Map internal coin name to Binance perp symbol."""
    return _K_MAP.get(coin, coin) + "USDT"


def fetch_bear_ohlcv(coin: str,
                     start_ms: int = BEAR_START_MS,
                     end_ms:   int = BEAR_END_MS) -> pd.DataFrame:
    """Fetch hourly OHLCV from Binance spot/perpetual for the bear window.

    Returns an empty DataFrame if the symbol is not listed on Binance or the
    requested window is entirely before the coin's listing date.
    """
    sym = binance_symbol(coin)
    rows, cur = [], start_ms
    while cur < end_ms:
        resp = requests.get(BINANCE_KLINES, params={
            "symbol": sym, "interval": "1h",
            "startTime": cur, "endTime": end_ms, "limit": 1000,
        }, timeout=30)
        if resp.status_code == 400:
            return pd.DataFrame()
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        rows.extend(data)
        cur = data[-1][0] + 1
        time.sleep(0.1)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])[["time", "open", "high", "low", "close", "volume"]]
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    # Trim to window
    df = df[(df["time"] >= pd.Timestamp(start_ms, unit="ms", tz="UTC")) &
            (df["time"] <  pd.Timestamp(end_ms,   unit="ms", tz="UTC"))]
    return df.sort_values("time").reset_index(drop=True)


def fetch_bear_funding(coin: str,
                       start_ms: int = BEAR_START_MS,
                       end_ms:   int = BEAR_END_MS) -> pd.DataFrame:
    """Paginate Binance USDⓈ-M futures funding rates over the bear window.

    Binance charges funding every 8h (00:00, 08:00, 16:00 UTC). The endpoint
    returns up to 1000 records per call; we paginate by fundingTime.

    Returns DataFrame with columns: fundingTime (UTC datetime), fundingRate (float).
    Empty DataFrame if symbol not in Binance perp universe.
    """
    sym = binance_symbol(coin)
    rows, cur = [], start_ms
    while cur < end_ms:
        resp = requests.get(BINANCE_FUNDING, params={
            "symbol": sym, "startTime": cur, "endTime": end_ms, "limit": 1000,
        }, timeout=30)
        if resp.status_code == 400:
            return pd.DataFrame()
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        rows.extend(data)
        if len(data) < 1000:
            break
        cur = data[-1]["fundingTime"] + 1
        time.sleep(0.1)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"].astype("int64"),
                                       unit="ms", utc=True)
    df["fundingRate"] = df["fundingRate"].astype(float)
    df = df[(df["fundingTime"] >= pd.Timestamp(start_ms, unit="ms", tz="UTC")) &
            (df["fundingTime"] <  pd.Timestamp(end_ms,   unit="ms", tz="UTC"))]
    return df[["fundingTime", "fundingRate"]].sort_values("fundingTime").reset_index(drop=True)


def ensure_bear_coin(coin: str, force: bool = False) -> tuple[bool, bool]:
    """Download and cache bear-era OHLCV and funding for `coin`.

    Returns (ohlcv_ok, funding_ok) indicating whether each file was obtained.
    Files are cached as data/bear_<COIN>_1h.csv and data/bear_<COIN>_funding.csv.
    """
    opath = DATA_DIR / f"bear_{coin}_1h.csv"
    fpath = DATA_DIR / f"bear_{coin}_funding.csv"

    ohlcv_ok = True
    if force or not opath.exists():
        df = fetch_bear_ohlcv(coin)
        if df.empty:
            ohlcv_ok = False
        else:
            df.to_csv(opath, index=False)
            print(f"  {coin}: ohlcv {len(df)} rows -> {opath.name}")
    else:
        ohlcv_ok = True  # already cached

    fund_ok = True
    if force or not fpath.exists():
        df = fetch_bear_funding(coin)
        if df.empty:
            fund_ok = False
        else:
            df.to_csv(fpath, index=False)
            print(f"  {coin}: funding {len(df)} rows -> {fpath.name}")
    else:
        fund_ok = True  # already cached

    return ohlcv_ok, fund_ok


def load_bear_ohlcv(coin: str) -> pd.DataFrame:
    """Load cached bear OHLCV. Returns empty if not cached."""
    p = DATA_DIR / f"bear_{coin}_1h.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True).dt.floor("h")
    return df.set_index("time")


def load_bear_funding(coin: str) -> pd.DataFrame:
    """Load cached bear funding. Returns empty if not cached."""
    p = DATA_DIR / f"bear_{coin}_funding.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], format="ISO8601", utc=True)
    return df.set_index("fundingTime")


if __name__ == "__main__":
    print(f"Bear basket: {BEAR_BASKET}")
    print(f"Window: {pd.Timestamp(BEAR_START_MS, unit='ms', tz='UTC').date()} → "
          f"{pd.Timestamp(BEAR_END_MS, unit='ms', tz='UTC').date()}")
    for coin in BEAR_BASKET:
        ok_o, ok_f = ensure_bear_coin(coin)
        if not ok_o:
            print(f"  {coin}: OHLCV NOT available on Binance")
        if not ok_f:
            print(f"  {coin}: funding NOT available on Binance perp")
