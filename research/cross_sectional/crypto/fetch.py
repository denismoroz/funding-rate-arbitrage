"""
HL perp data fetcher for the cross-sectional crypto universe.

Reuses the proven download patterns from research/fetch_ohlcv.py (Binance hourly
candles) and research/fetch_funding_history.py (HL paginated funding history).

Two public sources, no credentials:
  - HL  info endpoint  -> meta/asset-ctxs (liquidity ranking) + funding history
  - Binance klines     -> hourly OHLCV (HL serves only ~5000 recent candles, so
                          Binance is the long-history price source, as in the
                          existing research/data/*_1h.csv files)

Output (cached, idempotent) into ./data:
  <COIN>.csv      funding:  time, fundingRate, premium, annualizedPct
  <COIN>_1h.csv   ohlcv:    time, open, high, low, close, volume

Bridge tokens (AVAX0 / LINK0 / AAVE0 …, any name ending in a digit that aliases a
canonical perp) are NEVER fetched — project policy: independent price discovery.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

HL_URL      = "https://api.hyperliquid.xyz/info"
BINANCE_URL = "https://api.binance.com/api/v3/klines"

# History start used across the frab research dataset.
START_MS = int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)

# HL "kXXX" perps are 1000x-scaled; Binance lists them as "1000XXX".
# Price scale differs but cross-sectional returns/funding are scale-invariant.
_K_MAP = {"PEPE": "1000PEPE", "BONK": "1000BONK", "SHIB": "1000SHIB", "FLOKI": "1000FLOKI"}


def binance_symbol(coin: str) -> str:
    return _K_MAP.get(coin, coin) + "USDT"


def hl_meta_ctxs() -> list[dict]:
    """[{name, dayNtlVlm, oiUsd, markPx, isDelisted}] for the full HL perp universe."""
    r = requests.post(HL_URL, json={"type": "metaAndAssetCtxs"}, timeout=30)
    r.raise_for_status()
    meta, ctxs = r.json()
    out = []
    for u, c in zip(meta["universe"], ctxs):
        mark = float(c.get("markPx", 0) or 0)
        oi   = float(c.get("openInterest", 0) or 0)
        out.append({
            "name":       u["name"],
            "isDelisted": bool(u.get("isDelisted", False)),
            "dayNtlVlm":  float(c.get("dayNtlVlm", 0) or 0),
            "markPx":     mark,
            "oiUsd":      oi * mark,
        })
    return out


# ── Binance hourly OHLCV (long history price source) ──────────────────────────

def fetch_ohlcv(coin: str, start_ms: int = START_MS) -> pd.DataFrame:
    sym = binance_symbol(coin)
    rows, cur = [], start_ms
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    while cur < end:
        resp = requests.get(BINANCE_URL, params={
            "symbol": sym, "interval": "1h",
            "startTime": cur, "endTime": end, "limit": 1000,
        }, timeout=30)
        if resp.status_code == 400:
            # symbol not listed on Binance (HL-native coin, e.g. HYPE/FARTCOIN/SPX)
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
    return df.sort_values("time").reset_index(drop=True)


# ── HL funding history (paginated, 500/page) ──────────────────────────────────

def fetch_funding(coin: str, start_ms: int = START_MS) -> pd.DataFrame:
    rows, cur = [], start_ms
    while True:
        resp = requests.post(HL_URL, json={
            "type": "fundingHistory", "coin": coin, "startTime": cur,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        rows.extend(data)
        if len(data) < 500:
            break
        cur = data[-1]["time"] + 1
        time.sleep(0.2)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df["fundingRate"] = df["fundingRate"].astype(float)
    df["premium"] = df["premium"].astype(float)
    df["annualizedPct"] = df["fundingRate"] * 8760 * 100
    return df.sort_values("time").reset_index(drop=True)


# ── Idempotent cache ──────────────────────────────────────────────────────────

def _stale(path: Path, max_age_days: int = 2) -> bool:
    if not path.exists():
        return True
    try:
        last = pd.read_csv(path, usecols=["time"]).iloc[-1, 0]
        last = pd.to_datetime(last, format="ISO8601", utc=True)
    except Exception:
        return True
    return (pd.Timestamp.now(tz="UTC") - last).days > max_age_days


def ensure_coin(coin: str, refresh: bool = False) -> None:
    """Download + cache funding and OHLCV for `coin` if missing/stale."""
    fpath = DATA_DIR / f"{coin}.csv"
    opath = DATA_DIR / f"{coin}_1h.csv"
    if refresh or _stale(fpath):
        f = fetch_funding(coin)
        if not f.empty:
            f.to_csv(fpath, index=False)
            print(f"  {coin}: funding {len(f)} rows -> {fpath.name}")
    if refresh or _stale(opath):
        o = fetch_ohlcv(coin)
        if not o.empty:
            o.to_csv(opath, index=False)
            print(f"  {coin}: ohlcv {len(o)} rows -> {opath.name}")
