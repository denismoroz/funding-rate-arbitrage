"""Cross-exchange spread re-examined under research/SCREENING.md (2026-09-12).

Old verdict died on correlation +0.86 with FRAB ("the prize was FRAB
decorrelation"). New rules: correlation is sizing, not a filter. What is left:
  1) is the edge still there NOW (decay over time, fresh funding to 2026-09)?
  3) tail: legs sit on DIFFERENT venues, margin cannot jump between them, so each
     leg must survive the worst move alone -> capital needed -> return on capital.
Fresh funding is fetched by fetch_fresh.py into the scratchpad (not committed).
"""
import sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).parent
R = HERE.parent
for p in (R, R/"cross_sectional", R/"cross_sectional"/"crypto", R/"validation_harness", HERE):
    sys.path.insert(0, str(p))
from spread import build_spread_panel, portfolio_returns_spread
from characterize import trailing_direction_signal

FRESH = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/"
             "5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/spread_fresh")
COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "ARB", "OP"]
MM = 0.02
NET_OVER_GROSS = 0.946     # committed book: net 9.26 / gross 9.79
EVENT = (pd.Timestamp("2025-10-10 12:00", tz="UTC"), pd.Timestamp("2025-10-11 12:00", tz="UTC"))

def ann(s): return s.mean() * 365 * 100

print("== 1. decay of the committed book (HL-Binance trail lb90/rb21, net per notional) ==")
p = pd.read_csv(HERE/"characterize_committed_pnl.csv", parse_dates=["time"]).set_index("time")["spread_net"]
p = p[p.index >= p[p != 0].index[0]]
for y, g in p.groupby(p.index.year): print(f"  {y}: {ann(g):+6.2f}%")

print("\n== 1b. same config on FRESH funding ==")
panel = build_spread_panel((FRESH/"HL", 1), (FRESH/"Binance", 8), COINS)
sp = panel["spread"]
pnl = portfolio_returns_spread(trailing_direction_signal(sp, 90, 21), sp, 3.5, 5.0, 0.2)
pnl = pnl[pnl.index >= pd.Timestamp("2026-05-01", tz="UTC")]
print(f"  May-Sep 2026: {ann(pnl):+.2f}%  hit {(pnl > 0).mean()*100:.0f}%")
s2 = sp[sp.index >= pd.Timestamp("2026-05-01", tz="UTC")]
fresh = {c: s2[c].mean() * 3 * 365 * 100 for c in s2.columns}
print("  per-coin raw spread: " + "  ".join(f"{c}:{v:+.1f}" for c, v in fresh.items()))

print("\n== 3. return on capital with venue-separated margin (24h rebalance horizon) ==")
def worst(c, h=24, drop_event=False):
    df = pd.read_csv(R/"data"/f"{c}_1h.csv", parse_dates=["time"]).set_index("time")
    if drop_event: df = df[(df.index < EVENT[0]) | (df.index > EVENT[1])]
    up = (df["high"].rolling(h).max().shift(-h+1) / df["close"] - 1).max()
    dn = (1 - df["low"].rolling(h).min().shift(-h+1) / df["close"]).max()
    return max(up, dn)
def book(coins, drop_event=False):
    caps = [2 * (worst(c, 24, drop_event) + MM) for c in coins]
    net = np.mean([abs(fresh[c]) * NET_OVER_GROSS for c in coins])
    return net, np.mean(caps), net / np.mean(caps)
for lab, coins, de in (("all 9, honest", COINS, False), ("all 9, if 2025-10-10 never happened", COINS, True),
                       ("BTC+ETH, honest", ["BTC", "ETH"], False)):
    n, c, r = book(coins, de)
    print(f"  {lab:<38} net/notional {n:.2f}%  capital {c:.2f}x  RoC {r:.2f}%")
