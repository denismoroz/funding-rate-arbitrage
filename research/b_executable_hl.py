"""Strategy B as it could actually run on Hyperliquid (2026-09-13).
Model assumptions that do not hold on HL: TIA and INJ have no HL spot market; HL spot
BTC/ETH/SOL/AVAX are wrapped (Unit) tokens that earn no staking; idle USDC on HL earns
nothing unless moved to a lending venue. Re-price B + STICKY24 step by step, same common
window as b_sticky_returns.py, per $1000 of capital."""
import numpy as np, pandas as pd
from engine import STAKING_YIELD, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import build_trend_up
from b_sim_ext import simulate_ext
import b_entry_variants as V
from b_improve_ideas import sticky

WIN = (pd.Timestamp("2023-10-31", tz="UTC"), pd.Timestamp("2026-05-12 23:00", tz="UTC"))

def book(coins, staking_on, cash_apr, sticky_h=24):
    series = []
    for c in coins:
        df = V.data[c]; close = df["close"].values
        s = V.base(close); s = sticky(s, sticky_h) if sticky_h else s
        pnl, _ = simulate_ext(df, STAKING_YIELD.get(c, 0.0) if staking_on else 0.0, s, rebal_threshold=V.THR,
                              risk_free_apr=cash_apr, refill_confirm=build_trend_up(close), signal_lag=V.LAG,
                              slippage=V.SLIP)
        series.append(pd.Series(pnl, index=df.index))
    idx = sorted(set.intersection(*[set(x.index) for x in series]))
    idx = [t for t in idx if WIN[0] <= t <= WIN[1]]
    eq = TOTAL_CAPITAL + pd.concat(series, axis=1).loc[idx].mean(axis=1).cumsum()
    y = (eq.index[-1] - eq.index[0]).total_seconds()/3600/HOURS_PER_YEAR
    ann = ((eq.iloc[-1]/eq.iloc[0])**(1/y) - 1)*100
    dd = (eq/eq.cummax() - 1).min()*100
    yrs = {yr: (g.iloc[-1]/g.iloc[0]-1)*100 for yr, g in eq.groupby(eq.index.year)}
    return ann, dd, yrs

steps = [
    ("model: 6 coins, staking, cash 4%",         V.COINS, True, 0.04),
    ("HL coins only (no TIA/INJ), staking, 4%",   ["BTC","ETH","SOL","AVAX"], True, 0.04),
    ("HL coins, NO staking, cash 4%",             ["BTC","ETH","SOL","AVAX"], False, 0.04),
    ("HL coins, NO staking, cash 0% (pure HL)",   ["BTC","ETH","SOL","AVAX"], False, 0.0),
]
print(f"window {WIN[0]:%Y-%m-%d} .. {WIN[1]:%Y-%m-%d}, B + STICKY24\n")
print(f"{'assumptions':<44}{'per yr':>8}{'$/$1000':>9}{'maxDD':>8}   by year")
for lab, coins, stk, cash in steps:
    ann, dd, yrs = book(coins, stk, cash)
    print(f"{lab:<44}{ann:>+7.1f}%{ann*10:>+8.0f}{dd:>7.1f}%   " + " ".join(f"{y}:{v:+.1f}%" for y, v in yrs.items()))
ann, dd, yrs = book(["BTC","ETH","SOL","AVAX"], False, 0.0, sticky_h=0)
print(f"{'  (same pure-HL, WITHOUT sticky exit)':<44}{ann:>+7.1f}%{ann*10:>+8.0f}{dd:>7.1f}%   " + " ".join(f"{y}:{v:+.1f}%" for y, v in yrs.items()))
