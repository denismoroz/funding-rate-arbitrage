"""Why does HYST fail the second half? Raw numbers per coin and per half,
HYST vs baseline, plus the calendar split used in b_same_window.py."""
import numpy as np, pandas as pd
import b_entry_variants as V
from engine import STAKING_YIELD, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar, build_trend_up

def sim(c, sig):
    df = V.data[c]; close = df["close"].values
    pnl, info = simulate_constdollar(df, STAKING_YIELD.get(c, 0.0), sig, rebal_threshold=V.THR, risk_free_apr=V.CASH,
                                     refill_confirm=build_trend_up(close), signal_lag=V.LAG, slippage=V.SLIP)
    return pd.Series(pnl, index=df.index)

def seg(p):
    eq = TOTAL_CAPITAL + p.cumsum()
    ret = (eq.iloc[-1] / TOTAL_CAPITAL - 1) * 100
    dd = ((eq / eq.cummax()) - 1).min() * 100
    return ret, dd

print(f"{'coin':<5}{'half':<5}{'base ret':>10}{'base DD':>9}{'HYST ret':>10}{'HYST DD':>9}{'dates':>28}")
tot = {"b1": [], "h1": [], "b2": [], "h2": []}
for c in V.COINS:
    close = V.data[c]["close"].values
    pb, ph = sim(c, V.base(close)), sim(c, V.hyst(close))
    k = len(pb) // 2
    for lab, sl in (("H1", slice(0, k)), ("H2", slice(k, None))):
        rb, db = seg(pb.iloc[sl]); rh, dh = seg(ph.iloc[sl])
        d = pb.iloc[sl].index
        print(f"{c:<5}{lab:<5}{rb:>9.1f}%{db:>8.1f}%{rh:>9.1f}%{dh:>8.1f}%   {d[0]:%Y-%m}..{d[-1]:%Y-%m}")
        tot["b1" if lab == "H1" else "b2"].append(rb); tot["h1" if lab == "H1" else "h2"].append(rh)
print(f"\nmean return, H1: base {np.mean(tot['b1']):+.1f}%  HYST {np.mean(tot['h1']):+.1f}%")
print(f"mean return, H2: base {np.mean(tot['b2']):+.1f}%  HYST {np.mean(tot['h2']):+.1f}%")
