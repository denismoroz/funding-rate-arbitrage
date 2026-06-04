"""Per-coin funding attribution for expanded universe.

Goal: explain which coins drove the ~20-34% APR in margin-aware backtests
and which are dead weight in the expanded universe.
"""
from __future__ import annotations
import pandas as pd
from pathlib import Path

DATA = Path("/Users/d/prj/funding-rate-arbitrage/research/data")
COINS = ["BTC", "ETH", "SOL", "HYPE", "ZEC", "PURR", "XPL"]
LEVERAGE = {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10, "ZEC": 10, "PURR": 3, "XPL": 10}

# Strategy params (baseline that produced ~29% APR in margin sweep)
ENTRY = 0.10          # 10% annualized (live prod default)
POSITION_SIZE = 100
SIGNAL_WINDOW = 12    # 12h MA

# Capital cost per position = spot + (size / leverage) * 3 buffer
def capital_required(coin: str) -> float:
    return POSITION_SIZE + (POSITION_SIZE / LEVERAGE[coin]) * 3.0


def load(coin: str) -> pd.DataFrame:
    # Check both .csv and _1h.csv to be robust
    path = DATA / f"{coin}.csv"
    if not path.exists():
        path = DATA / f"{coin}_1h.csv"
        
    if not path.exists():
        print(f"Warning: Data for {coin} not found at {path}")
        return pd.DataFrame()

    df = pd.read_csv(path, parse_dates=["time"])
    df = df.sort_values("time").set_index("time")
    df["sig_ma12"] = df["fundingRate"].rolling(SIGNAL_WINDOW).mean() * 8760
    return df


def windows():
    return {
        "full (2023-06 → 2026-05)": ("2023-06-01", "2026-05-31"),
        "2024 (hot)":                ("2024-01-01", "2024-12-31"),
        "2025":                       ("2025-01-01", "2025-12-31"),
        "last_180d":                  ("2025-12-01", "2026-05-31"),
        "last_90d":                   ("2026-03-01", "2026-05-31"),
        "last_30d":                   ("2026-05-01", "2026-05-31"),
    }


def analyze(coin: str, df: pd.DataFrame, start: str, end: str) -> dict:
    if df.empty:
        return {}
    
    sub = df.loc[start:end]
    if sub.empty:
        return {}
        
    funding_hourly = sub["fundingRate"]
    sig = sub["sig_ma12"].dropna()
    cap_req = capital_required(coin)

    # Pct of hours signal exceeds entry threshold (would trigger open if slot free)
    hot_pct = (sig > ENTRY).mean() * 100 if len(sig) else 0
    # Mean & median annualized funding
    annualized = funding_hourly * 8760
    # Expected APR if held continuously: mean funding * (notional / capital_req)
    # capital efficiency factor = position_size / capital_req
    cap_eff = POSITION_SIZE / cap_req
    expected_apr_if_held = annualized.mean() * cap_eff  # short-perp captures this on $POSITION_SIZE
    return {
        "coin": coin,
        "lev": LEVERAGE[coin],
        "cap_per_pos": round(cap_req, 1),
        "cap_eff": round(cap_eff, 2),
        "mean_apr_%": round(annualized.mean(), 2),
        "median_apr_%": round(annualized.median(), 2),
        "p95_apr_%": round(annualized.quantile(0.95), 2),
        "hot_hours_%": round(hot_pct, 1),
        "expected_apr_on_cap_%": round(expected_apr_if_held, 2),
    }


def main():
    data = {c: load(c) for c in COINS}
    # Filter out coins for which data couldn't be loaded
    available_coins = [c for c in COINS if not data[c].empty]
    
    for wname, (start, end) in windows().items():
        print(f"\n=== {wname} ===")
        rows = [analyze(c, data[c], start, end) for c in available_coins]
        rows = [r for r in rows if r]
        if not rows:
            print("No data available for this window.")
            continue
        df = pd.DataFrame(rows)
        df = df.sort_values("expected_apr_on_cap_%", ascending=False)
        print(df.to_string(index=False))

    # Aggregate: contribution to portfolio expected return
    print("\n=== Portfolio contribution (full period, equal hold time) ===")
    print("Assumes each coin holds position equally often; ranks by ann.funding × cap_eff")
    # This part was an empty print in the original, adding a placeholder logic for utility
    # if we were to calculate it. For now, it just identifies the coins used.
    print(f"Analyzed keys: {available_coins}")


if __name__ == "__main__":
    main()
