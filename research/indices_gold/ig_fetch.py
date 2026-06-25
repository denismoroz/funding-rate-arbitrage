"""
Indices + Gold/Silver data fetcher (Yahoo Finance chart API, no API key).

Universe: 11 equity index levels + 2 metals ETFs.  Only SPOT/LEVEL series are
needed (no carry rates, no REER) — TSMOM is a pure price-trend strategy.

  SP500=^GSPC  NASDAQ=^IXIC  DOW=^DJI  RUSSELL2K=^RUT
  FTSE=^FTSE   DAX=^GDAXI   CAC=^FCHI  NIKKEI=^N225
  HANGSENG=^HSI  ASX200=^AXJO  TSX=^GSPTSE
  GLD  SLV

Raw CSVs are cached idempotently to ./data/ (gitignored by the repo `data/`
rule — do NOT commit them). fetch_all is idempotent: skips existing files
unless refresh=True.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
TIMEOUT = 30
SLEEP = 0.4  # polite gap between Yahoo requests

# ── Universe ─────────────────────────────────────────────────────────────────
# (name → Yahoo Finance symbol)
SYMBOLS: dict[str, str] = {
    "SP500":      "^GSPC",
    "NASDAQ":     "^IXIC",
    "DOW":        "^DJI",
    "RUSSELL2K":  "^RUT",
    "FTSE":       "^FTSE",
    "DAX":        "^GDAXI",
    "CAC":        "^FCHI",
    "NIKKEI":     "^N225",
    "HANGSENG":   "^HSI",
    "ASX200":     "^AXJO",
    "TSX":        "^GSPTSE",
    "GLD":        "GLD",
    "SLV":        "SLV",
}

ASSETS = list(SYMBOLS.keys())


# ── Fetcher ───────────────────────────────────────────────────────────────────

def fetch_spot_yahoo(asset: str, session: requests.Session | None = None,
                     range_: str = "30y") -> pd.DataFrame:
    """Daily close from Yahoo Finance chart API -> DataFrame[date, close].
    https://query1.finance.yahoo.com/v8/finance/chart/<sym>?range=30y&interval=1d
    All assets are already in USD (indices in local-currency points but we treat
    them as USD for TSMOM — signal is purely the TREND of each asset's own series,
    not cross-currency comparisons).
    """
    s = session or requests.Session()
    s.headers.update({"User-Agent": UA})
    sym = SYMBOLS[asset]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    r = s.get(url, params={"range": range_, "interval": "1d"}, timeout=TIMEOUT)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    close = res["indicators"]["quote"][0]["close"]
    out = pd.DataFrame({
        "date":  pd.to_datetime(ts, unit="s", utc=True).normalize(),
        "close": pd.Series(close, dtype="float64"),
    })
    return out.dropna().sort_values("date").reset_index(drop=True)


def _path(asset: str) -> Path:
    return DATA_DIR / f"spot_{asset}.csv"


def fetch_all(refresh: bool = False) -> None:
    """Download + cache spot for the full universe.  Skips existing files unless
    refresh=True.  All series via Yahoo Finance chart API (no key required)."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    print("=== FETCH: spot (Yahoo Finance chart API) ===")
    for asset in ASSETS:
        p = _path(asset)
        if p.exists() and not refresh:
            print(f"  {asset:<12}: cached ({p.name})")
            continue
        try:
            df = fetch_spot_yahoo(asset, sess)
            if df.empty:
                print(f"  {asset:<12}: EMPTY — no data returned")
                continue
            df.to_csv(p, index=False)
            print(f"  {asset:<12}: {len(df)} rows  "
                  f"{df['date'].min().date()} -> {df['date'].max().date()}  "
                  f"-> {p.name}")
        except Exception as e:
            print(f"  {asset:<12}: FAILED ({type(e).__name__}: {e})")
        time.sleep(SLEEP)


if __name__ == "__main__":
    import sys
    fetch_all(refresh="--refresh" in sys.argv)
