"""Strategy B re-check (2026-09-13): why 44 / 22 / 6 look contradictory.

1. They are three different periods AND methods (artifacts / honest full period /
   walk-forward OOS windows), not one number measured three times.
2. B is NOT fully invested: engine TOTAL_CAPITAL = $2000, B holds $1000 spot and
   $1000 cash at 4%; buy-and-hold holds $2000 spot. Comparing B to 100% holding
   mixes up hedge skill with simply owning half as much market.
   Fair benchmark = 50% spot + 50% cash, same coins, no hedge (NOHEDGE below).
One continuous simulation per coin, sliced by window; window returns are measured
from equity at the window's start (not from the initial $2000).
"""
import numpy as np, pandas as pd
from engine import STAKING_YIELD, load_data, buy_and_hold, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar, build_trend_up

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
CASH, SLIP, LAG, THR = 0.04, 0.0005, 1, 0.20

def hedge_signal(close):
    s = pd.Series(close)
    return (s.pct_change(14*24).fillna(0).values < 0) | (s.pct_change(30*24).fillna(0).values < 0)

cols, dec = {}, []
for coin in COINS:
    df = load_data(coin); close = df["close"].values; stk = STAKING_YIELD.get(coin, 0.0)
    pnl, info = simulate_constdollar(df, stk, hedge_signal(close), rebal_threshold=THR, risk_free_apr=CASH,
                                     refill_confirm=build_trend_up(close), signal_lag=LAG, slippage=SLIP)
    bh = buy_and_hold(df, stk)
    cols[coin] = pd.DataFrame({"B": pnl, "HOLD": bh,
                               "NOHEDGE": 0.5*bh + (TOTAL_CAPITAL/2)*CASH/HOURS_PER_YEAR}, index=df.index)
    yrs = len(df)/HOURS_PER_YEAR
    dec.append({k: info[v]/TOTAL_CAPITAL/yrs*100 for k, v in
                (("hedge", "short_realized_pnl"), ("funding", "funding_total"))} |
               {"fees": -(info["perp_fees_total"]+info["spot_fees_total"])/TOTAL_CAPITAL/yrs*100, "coin": coin})
idx = sorted(set.intersection(*[set(v.index) for v in cols.values()]))
avg = {k: pd.concat([cols[c][k] for c in COINS], axis=1).loc[idx].mean(axis=1) for k in ("B", "HOLD", "NOHEDGE")}
eq = {k: TOTAL_CAPITAL + v.cumsum() for k, v in avg.items()}
start, end = idx[0], idx[-1]
print(f"common data {start:%Y-%m-%d} .. {end:%Y-%m-%d}; equal-weight {', '.join(COINS)}; $2000 per coin\n")

def win(k, a, b):
    e = eq[k][(eq[k].index >= a) & (eq[k].index <= b)]
    ret = e.iloc[-1]/e.iloc[0] - 1
    yrs = (e.index[-1]-e.index[0]).total_seconds()/3600/HOURS_PER_YEAR
    ann = ((1+ret)**(1/yrs)-1)*100 if yrs >= 0.99 else None
    dd = (e/e.cummax()-1).min()*100
    return ret*100, ann, dd
oos0 = start + pd.Timedelta(hours=12*730)
W = [("full period", start, end), ("first 12 months", start, oos0), ("after month 12 = OOS span", oos0, end)]
W += [(f"calendar {y}", max(start, pd.Timestamp(f"{y}-01-01", tz="UTC")), min(end, pd.Timestamp(f"{y}-12-31 23:00", tz="UTC")))
      for y in sorted(set(pd.DatetimeIndex(idx).year))]
print(f"{'window':<28}{'B':>16}{'50% spot+cash':>18}{'100% hold':>16}   B vs 50/50")
print(f"{'':<28}{'return / DD':>16}{'return / DD':>18}{'return / DD':>16}")
for lab, a, b in W:
    r = {k: win(k, a, b) for k in ("B", "NOHEDGE", "HOLD")}
    f = lambda t: f"{t[0]:+6.1f}% /{t[2]:4.0f}%"
    print(f"{lab:<28}{f(r['B']):>16}{f(r['NOHEDGE']):>18}{f(r['HOLD']):>16}   {r['B'][0]-r['NOHEDGE'][0]:+6.1f}pp"
          + (f"   (B ann {r['B'][1]:+.1f}%)" if r['B'][1] is not None else ""))
d = pd.DataFrame(dec).set_index("coin")
print("\nwhere B's extra money comes from, %/yr of capital (per coin, own full history):")
print(d.round(2).to_string())
print(f"  mean: hedge {d.hedge.mean():+.2f}  funding {d.funding.mean():+.2f}  fees {d.fees.mean():+.2f}")
