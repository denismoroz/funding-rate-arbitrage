"""Robustness of STICKY exit (2026-09-13). NOT a parameter search: the pre-registered
value stays 24h. Question: is 24h a knife-edge, or does the effect hold for neighbours?
Plus per-coin / per-half raw numbers and a post-hoc stack with MAKER (labelled)."""
import numpy as np, pandas as pd
from engine import STAKING_YIELD, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import build_trend_up
from b_sim_ext import simulate_ext
import b_entry_variants as V
from b_improve_ideas import sticky, mom30

def sim(c, sig, slip=V.SLIP):
    df = V.data[c]; close = df["close"].values
    pnl, info = simulate_ext(df, STAKING_YIELD.get(c, 0.0), sig, rebal_threshold=V.THR, risk_free_apr=V.CASH,
                             refill_confirm=build_trend_up(close), signal_lag=V.LAG, slippage=slip)
    return pnl, info, len(df)/HOURS_PER_YEAR

def st(p):
    eq = TOTAL_CAPITAL + np.cumsum(p); y = len(p)/HOURS_PER_YEAR
    return ((eq[-1]/TOTAL_CAPITAL)**(1/y)-1)*100, -((eq/np.maximum.accumulate(eq))-1).min()*100

def book(entry_fn, hours, slip=V.SLIP):
    r = []
    for c in V.COINS:
        close = V.data[c]["close"].values; s = entry_fn(close)
        if hours: s = sticky(s, hours)
        p, info, y = sim(c, s, slip); h = len(p)//2
        r.append(dict(full=st(p), h1=st(p[:h]), h2=st(p[h:]), fees=-(info["perp_fees_total"]+info["spot_fees_total"])/TOTAL_CAPITAL/y*100,
                      trades=info["trades"]/y, on=float(np.mean(s))*100))
    m = lambda k, j: np.mean([x[k][j] for x in r])
    return dict(CAGR=m("full",0), DD=m("full",1), Calmar=m("full",0)/m("full",1), Cal_H1=m("h1",0)/m("h1",1),
                Cal_H2=m("h2",0)/m("h2",1), DD_H2=m("h2",1), fees=np.mean([x["fees"] for x in r]),
                trades=np.mean([x["trades"] for x in r]), hedged=np.mean([x["on"] for x in r])), r

print("1) NEIGHBOURHOOD of the exit confirmation (entry immediate)")
rows = {}
for e_lab, e_fn in (("BASE", V.base), ("MOM30", mom30)):
    for hrs in (0, 6, 12, 24, 48, 72, 120):
        rows[(e_lab, hrs)] = book(e_fn, hrs)[0]
T = pd.DataFrame(rows).T; T.index.names = ["entry", "sticky_h"]
print(T.round(2).to_string())

print("\n2) PER COIN / PER HALF, BASE vs BASE+STICKY24")
_, rb = book(V.base, 0); _, rs = book(V.base, 24)
print(f"{'coin':<5}{'half':<5}{'base ret':>10}{'base DD':>9}{'sticky ret':>12}{'sticky DD':>11}")
for c, a, b in zip(V.COINS, rb, rs):
    for hl in ("h1", "h2"):
        print(f"{c:<5}{hl:<5}{a[hl][0]:>9.1f}%{a[hl][1]:>8.1f}%{b[hl][0]:>11.1f}%{b[hl][1]:>10.1f}%")

print("\n3) POST-HOC STACK (not pre-registered): BASE + STICKY24 + MAKER")
for lab, hrs, slip in (("BASE", 0, V.SLIP), ("BASE+MAKER", 0, 0.0002), ("BASE+STICKY24", 24, V.SLIP),
                       ("BASE+STICKY24+MAKER", 24, 0.0002)):
    r = book(V.base, hrs, slip)[0]
    print(f"  {lab:<22} CAGR {r['CAGR']:5.1f}%  DD {r['DD']:5.1f}%  Calmar {r['Calmar']:.2f}  H2 DD {r['DD_H2']:5.1f}%  fees {r['fees']:+.1f}%")
