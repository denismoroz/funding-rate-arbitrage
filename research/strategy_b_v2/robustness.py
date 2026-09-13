"""Last pre-registered check + money view for the SELECTED config (2026-09-13).
1) start-path robustness: shift the simulation start 0..27 days (7 paths), compare
   SELECTED vs CURRENT on TEST (the pre-registered bar: better on >= 6/7);
2) calendar years and $ per $1000 on the research period, forward separately;
3) execution upside: the same SELECTED config with maker-ish 2 bps slippage."""
import numpy as np, pandas as pd
import harness as H

SEL = dict(sticky=12, a=0.0, thr=0.50, carry=True, weights="equal")
CUR = H.CURRENT
data_r, data_f = H.load("research"), H.load("forward")

def port(data, cfg, slip=H.SLIP, start_h=0):
    eq = H.coin_equity(data, cfg["sticky"], cfg["a"], cfg["thr"], carry=False, slip=slip, start_h=start_h)
    if cfg["carry"]:
        add = {c: pd.Series(np.cumsum(H.carry_pnl(data[c].iloc[start_h:], slip)), index=data[c].index[start_h:]).resample("D").last()
               for c in H.COINS}
        eq = eq.add(pd.DataFrame(add).reindex(eq.index).ffill().fillna(0.0), fill_value=0.0)
    return H.portfolio(eq, cfg["weights"])

print("1) START PATHS — TEST segment (2025-06..2026-05)")
wins = 0
for d in (0, 3, 7, 11, 16, 21, 27):
    s = H.seg(port(data_r, SEL, start_h=d*24), *H.TEST); c = H.seg(port(data_r, CUR, start_h=d*24), *H.TEST)
    wins += s["ret"] > c["ret"]
    print(f"   shift {d:>2}d: SELECTED {s['ret']:+5.2f}% DD {s['dd']:4.1f}%  |  CURRENT {c['ret']:+5.2f}% DD {c['dd']:4.1f}%")
print(f"   SELECTED better on {wins}/7 paths  ->  {'PASS' if wins >= 6 else 'FAIL'} (pre-registered bar 6/7)")

print("\n2) MONEY — per $1000, calendar years (research data) and forward (fresh HL data)")
for lab, cfg, slip in (("CURRENT (taker)", CUR, H.SLIP), ("SELECTED (taker)", SEL, H.SLIP), ("SELECTED (maker 2bp)", SEL, 0.0002)):
    rp = port(data_r, cfg, slip); fw = port(data_f, cfg, slip)
    yrs = {y: ((1+g).prod()-1)*100 for y, g in rp.groupby(rp.index.year)}
    f = H.seg(fw, *H.FWD); full = H.seg(rp, rp.index[0], rp.index[-1])
    print(f"   {lab:<22} full {full['ann']:+5.1f}%/yr (${full['ann']*10:+.0f}) DD {full['dd']:4.1f}%   "
          + "  ".join(f"{y}: {v:+5.1f}% (${v*10:+.0f})" for y, v in yrs.items())
          + f"   | FWD {f['ret']:+5.2f}% (${f['ret']*10:+.0f}) DD {f['dd']:.1f}%")

print("\n3) WHERE THE MONEY COMES FROM (SELECTED, research period): B book vs carry overlay")
eqB = H.coin_equity(data_r, SEL["sticky"], SEL["a"], SEL["thr"], carry=False)
carry = {c: H.carry_pnl(data_r[c]) for c in H.COINS}
years = (eqB.index[-1]-eqB.index[0]).days/365
for c in H.COINS:
    b = (eqB[c].iloc[-1]-H.TOTAL_CAPITAL)/H.TOTAL_CAPITAL/years*100
    k = carry[c].sum()/H.TOTAL_CAPITAL/years*100
    print(f"   {c:<5} B book {b:+5.1f}%/yr   carry overlay {k:+5.2f}%/yr")
