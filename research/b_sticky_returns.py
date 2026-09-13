"""What return does Strategy B actually make with the sticky exit? (2026-09-13)
Same equal-weight 6-coin book and common window as b_same_window.py, window returns from
equity at window start. Variants: current, +STICKY24, +STICKY24+MAKER; references: 50% spot
+ 50% cash without hedge, and 100% hold. Money shown per $1000 of B capital."""
import numpy as np, pandas as pd
from engine import STAKING_YIELD, load_data, buy_and_hold, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import build_trend_up
from b_sim_ext import simulate_ext
import b_entry_variants as V
from b_improve_ideas import sticky

cols = {}
for c in V.COINS:
    df = V.data[c]; close = df["close"].values; stk = STAKING_YIELD.get(c, 0.0)
    kw = dict(rebal_threshold=V.THR, risk_free_apr=V.CASH, refill_confirm=build_trend_up(close), signal_lag=V.LAG)
    base = V.base(close)
    cur, _ = simulate_ext(df, stk, base, slippage=V.SLIP, **kw)
    stk24, _ = simulate_ext(df, stk, sticky(base, 24), slippage=V.SLIP, **kw)
    stkmk, _ = simulate_ext(df, stk, sticky(base, 24), slippage=0.0002, **kw)
    bh = buy_and_hold(df, stk)
    cols[c] = pd.DataFrame({"current": cur, "sticky": stk24, "sticky+maker": stkmk,
                            "50/50 no hedge": 0.5*bh + (TOTAL_CAPITAL/2)*V.CASH/HOURS_PER_YEAR, "100% hold": bh}, index=df.index)
idx = sorted(set.intersection(*[set(v.index) for v in cols.values()]))
names = list(cols[V.COINS[0]].columns)
eq = {k: TOTAL_CAPITAL + pd.concat([cols[c][k] for c in V.COINS], axis=1).loc[idx].mean(axis=1).cumsum() for k in names}
start, end = idx[0], idx[-1]
def win(k, a, b):
    e = eq[k][(eq[k].index >= a) & (eq[k].index <= b)]
    r = e.iloc[-1]/e.iloc[0]-1; y = (e.index[-1]-e.index[0]).total_seconds()/3600/HOURS_PER_YEAR
    return r*100, (((1+r)**(1/y)-1)*100 if y >= 0.99 else None), (e/e.cummax()-1).min()*100
oos0 = start + pd.Timedelta(hours=12*730)
W = [("full period", start, end), ("first 12 months (bull)", start, oos0), ("after month 12 (bear)", oos0, end)]
W += [(f"{y}", max(start, pd.Timestamp(f"{y}-01-01", tz="UTC")), min(end, pd.Timestamp(f"{y}-12-31 23:00", tz="UTC")))
      for y in sorted(set(pd.DatetimeIndex(idx).year))]
print(f"common window {start:%Y-%m-%d} .. {end:%Y-%m-%d}; cells = return over the window / max drawdown\n")
print(f"{'window':<24}" + "".join(f"{n:>20}" for n in names))
for lab, a, b in W:
    cells = []
    for n in names:
        r, ann, dd = win(n, a, b)
        cells.append(f"{r:+6.1f}% /{dd:4.0f}%")
    print(f"{lab:<24}" + "".join(f"{c:>20}" for c in cells))
print("\nannualised over the full period, and $ per year per $1000 of capital:")
for n in names:
    r, ann, dd = win(n, start, end)
    print(f"  {n:<16} {ann:+5.1f}%/yr  -> ${ann*10:+.0f}/yr per $1000,  max drawdown {dd:.0f}% (${dd*10:.0f})")
