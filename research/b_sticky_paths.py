"""After the XSMOM anchor lesson: is B's STICKY24 gain one lucky path? (2026-09-13)
B is hourly with no weekly anchor, so the analogous path risk is where the simulation
starts. Re-run BASE vs BASE+STICKY24 with the start shifted by 0..27 days (7 paths)."""
import numpy as np, pandas as pd
from engine import STAKING_YIELD, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import build_trend_up
from b_sim_ext import simulate_ext
import b_entry_variants as V
from b_improve_ideas import sticky

def book(start_h, hours):
    cg, dd = [], []
    for c in V.COINS:
        df = V.data[c].iloc[start_h:]; close = df["close"].values
        s = V.base(close); s = sticky(s, hours) if hours else s
        pnl, _ = simulate_ext(df, STAKING_YIELD.get(c, 0.0), s, rebal_threshold=V.THR, risk_free_apr=V.CASH,
                              refill_confirm=build_trend_up(close), signal_lag=V.LAG, slippage=V.SLIP)
        eq = TOTAL_CAPITAL + np.cumsum(pnl); y = len(pnl)/HOURS_PER_YEAR
        cg.append(((eq[-1]/TOTAL_CAPITAL)**(1/y)-1)*100); dd.append(-((eq/np.maximum.accumulate(eq))-1).min()*100)
    return np.mean(cg), np.mean(dd), np.mean(cg)/np.mean(dd)

print(f"{'start shift':<12}{'current CAGR/DD/Calmar':>28}{'+STICKY24 CAGR/DD/Calmar':>30}")
wins = 0
for d in (0, 3, 7, 11, 16, 21, 27):
    a = book(d*24, 0); b = book(d*24, 24); wins += b[2] > a[2]
    print(f"{str(d)+' days':<12}{a[0]:>10.1f}%{a[1]:>7.1f}%{a[2]:>9.2f}{b[0]:>12.1f}%{b[1]:>7.1f}%{b[2]:>9.2f}")
print(f"\nSTICKY24 better Calmar on {wins}/7 start paths")
