"""Hyperliquid vs Backpack funding-rate regime comparison.

Backpack data starts 2025-01 → only the COLD regime (2025-01-01 → 2026-04-01)
overlaps with our HL history; there is no hot-window Backpack data.

Backpack funding is hourly, but the early-2025 dump has sub-hourly (10-min)
rows — we resample to hourly mean before annualizing (×8760) so the cadence
artifact does not 6× inflate the annualized figure. HL cold-window data is
already hourly with annualized_pct precomputed.

Output: console table + regime_comparison.csv (schema matches drift/dydx).
Research only.
"""
import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
HL_DIR = HERE.parent / "drift" / "funding_history_hl"
BP_DIR = HERE.parent / "data_backpack"

# overlap of Backpack markets ∩ HL funding history
COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE"]

COLD_START = pd.Timestamp("2025-01-01", tz="UTC")
COLD_END = pd.Timestamp("2026-04-01", tz="UTC")


def load_hl(coin: str) -> pd.Series:
    """HL hourly annualized %, indexed by UTC hour, cold window only."""
    df = pd.read_csv(HL_DIR / f"{coin}.csv")
    df["t"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("t").sort_index()
    s = df["annualized_pct"].loc[COLD_START:COLD_END]
    return s.resample("1h").mean().dropna()


def load_bp(coin: str) -> pd.Series:
    """Backpack hourly annualized %, resampled from raw rate × 8760."""
    df = pd.read_csv(BP_DIR / f"{coin}.csv")
    df["t"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("t").sort_index()
    hourly = df["fundingRate"].resample("1h").mean().dropna()
    ann = hourly * 8760 * 100.0
    return ann.loc[COLD_START:COLD_END]


def main():
    rows = []
    print(f"COLD regime {COLD_START.date()} → {COLD_END.date()}  "
          f"(Backpack has no hot-window data)\n")
    print(f"{'coin':>5}{'HL cold%':>10}{'BP cold%':>10}{'Δ(BP-HL)':>11}"
          f"{'BP>HL %hrs':>12}{'BP neg%':>9}{'HL neg%':>9}")
    for c in COINS:
        hl = load_hl(c)
        bp = load_bp(c)
        # align on common hours for the head-to-head columns
        j = pd.concat({"hl": hl, "bp": bp}, axis=1).dropna()
        hl_m = hl.mean()
        bp_m = bp.mean()
        bp_win = 100.0 * (j["bp"] > j["hl"]).mean() if len(j) else float("nan")
        bp_neg = 100.0 * (bp < 0).mean()
        hl_neg = 100.0 * (hl < 0).mean()
        print(f"{c:>5}{hl_m:>10.2f}{bp_m:>10.2f}{bp_m-hl_m:>+11.2f}"
              f"{bp_win:>11.1f}%{bp_neg:>8.1f}%{hl_neg:>8.1f}%")
        rows.append({
            "coin": c, "hl_cold": round(hl_m, 4), "bp_cold": round(bp_m, 4),
            "delta_cold": round(bp_m - hl_m, 4),
            "bp_gt_hl_pct_hours": round(bp_win, 2),
            "bp_neg_pct": round(bp_neg, 2), "hl_neg_pct": round(hl_neg, 2),
            "n_common_hours": len(j),
        })
    out = pd.DataFrame(rows)
    out.to_csv(HERE / "regime_comparison.csv", index=False)
    print(f"\nportfolio mean — HL {out['hl_cold'].mean():.2f}%  "
          f"Backpack {out['bp_cold'].mean():.2f}%  "
          f"Δ {out['bp_cold'].mean()-out['hl_cold'].mean():+.2f}%")
    print(f"\nwrote {HERE/'regime_comparison.csv'}")


if __name__ == "__main__":
    main()
