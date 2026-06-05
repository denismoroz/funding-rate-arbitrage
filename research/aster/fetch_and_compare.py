"""Aster DEX (asterdex) funding fetch + HL comparison.

Aster is a Binance-fork perp DEX (BNB ecosystem), 8h funding interval.
Endpoint: GET /fapi/v1/fundingRate?symbol=&startTime=&limit=1000  (Binance-style).
Covers all 7 of our universe coins INCLUDING HYPE (rare off-HL).

Annualization is cadence-agnostic via `hourly_annualized` (rate / interval_hours,
spread over the hours it covers) rather than a hardcoded ×1095. Aster is uniformly
8h today, so both agree — but if the venue ever changes cadence (as Backpack did:
8h→hourly), the per-interval method stays correct instead of silently mis-scaling.

Cold regime 2025-01-01 → 2026-04-01 vs HL (research/drift/funding_history_hl/).
Writes funding_history/<COIN>.csv + regime_comparison.csv. Research only.
"""
import time
import datetime
from pathlib import Path

import requests
import pandas as pd

HERE = Path(__file__).parent
HL_DIR = HERE.parent / "drift" / "funding_history_hl"
FH_DIR = HERE / "funding_history"
FH_DIR.mkdir(exist_ok=True)

API = "https://fapi.asterdex.com/fapi/v1/fundingRate"

COINS = ["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK", "DOGE"]
SYMBOL = {c: f"{c}USDT" for c in COINS}

START_MS = int(datetime.datetime(2024, 6, 1).timestamp() * 1000)
COLD_START = pd.Timestamp("2025-01-01", tz="UTC")
COLD_END = pd.Timestamp("2026-04-01", tz="UTC")


def fetch(coin: str) -> pd.DataFrame:
    out = FH_DIR / f"{coin}.csv"
    if out.exists():
        return pd.read_csv(out)
    recs, start = [], START_MS
    while True:
        r = requests.get(API, params={"symbol": SYMBOL[coin], "startTime": start,
                                       "limit": 1000}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        recs.extend(data)
        last = data[-1]["fundingTime"]
        if len(data) < 1000:
            break
        start = last + 1
        time.sleep(0.15)
    if not recs:
        print(f"  {coin}: EMPTY")
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    df["fundingRate"] = df["fundingRate"].astype(float)
    df = df[["fundingTime", "fundingRate"]].drop_duplicates("fundingTime")
    df = df.sort_values("fundingTime").reset_index(drop=True)
    df.to_csv(out, index=False)
    return df


def hourly_annualized(rate: pd.Series) -> pd.Series:
    """Cadence-agnostic hourly annualized-% series from per-settlement rates.

    Converts each settlement to a per-hour equivalent (rate / interval_hours) and
    spreads it across the hours it covers, so ×8760 is correct for ANY interval.
    A fixed periods-per-year multiplier silently mis-scales if the venue changes
    cadence — the rake Backpack's 8h→hourly switch exposed.

    interval_hours = gap to the NEXT settlement, clipped to [1, 8].
    """
    rate = rate.sort_index()
    gap_h = rate.index.to_series().diff().shift(-1).dt.total_seconds() / 3600.0
    gap_h = gap_h.fillna(gap_h.median()).clip(lower=1.0, upper=8.0)
    hourly_equiv = rate / gap_h.values
    hourly = hourly_equiv.resample("1h").ffill().dropna()
    return hourly * 8760 * 100.0


def load_hl_cold(coin: str) -> pd.Series:
    p = HL_DIR / f"{coin}.csv"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p)
    df["t"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    s = df.set_index("t").sort_index()["annualized_pct"].loc[COLD_START:COLD_END]
    return s


def aster_cold(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    t = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    rate = pd.Series(df["fundingRate"].values, index=t)
    return hourly_annualized(rate).loc[COLD_START:COLD_END]


def main():
    print("fetching Aster funding (8h, all 7 coins incl HYPE)...")
    dfs = {c: fetch(c) for c in COINS}
    for c, df in dfs.items():
        n = len(df)
        first = pd.to_datetime(df["fundingTime"].iloc[0], unit="ms").date() if n else "—"
        print(f"  {c}: {n} records from {first}")

    print(f"\nCOLD regime {COLD_START.date()} → {COLD_END.date()}\n")
    print(f"{'coin':>5}{'HL cold%':>10}{'Aster cold%':>13}{'Δ(Ast-HL)':>12}{'Ast neg%':>10}")
    rows = []
    for c in COINS:
        hl = load_hl_cold(c)
        ast = aster_cold(dfs[c])
        hl_m = hl.mean() if len(hl) else float("nan")
        ast_m = ast.mean() if len(ast) else float("nan")
        ast_neg = 100.0 * (ast < 0).mean() if len(ast) else float("nan")
        d = ast_m - hl_m
        print(f"{c:>5}{hl_m:>10.2f}{ast_m:>13.2f}{d:>+12.2f}{ast_neg:>9.1f}%")
        rows.append({"coin": c, "hl_cold": round(hl_m, 4), "aster_cold": round(ast_m, 4),
                     "delta_cold": round(d, 4), "aster_neg_pct": round(ast_neg, 2),
                     "aster_records": len(dfs[c])})
    out = pd.DataFrame(rows)
    out.to_csv(HERE / "regime_comparison.csv", index=False)
    excl_hype = out[out.coin != "HYPE"]
    print(f"\nportfolio mean (excl HYPE) — HL {excl_hype['hl_cold'].mean():.2f}%  "
          f"Aster {excl_hype['aster_cold'].mean():.2f}%")
    print(f"wrote {HERE/'regime_comparison.csv'}")


if __name__ == "__main__":
    main()
