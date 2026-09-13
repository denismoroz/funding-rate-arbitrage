"""Sticky exit applied to trend TSMOM (2026-09-13).
Trend bars are daily, so the analogue of B's 24h is 1 day. Rule per coin: opening a
position from flat is immediate; closing or flipping it waits N EXTRA days of a disagreeing sign
(N = 1 is the analogue of B's 24h; 0 = current behaviour, 2 shown as a neighbour).
Note: a first version counted the disagreeing day itself, which made N=1 a no-op.
Two views: (a) the frictionless-with-costs full book (scale-free Sharpe, costs, turnover);
(b) the realistic $1000 majors account from sim_1000.py."""
import sys
from pathlib import Path
import numpy as np, pandas as pd
R = Path("/Users/d/prj/funding-rate-arbitrage/research")
for p in (R, R/"validation_harness", R/"cross_sectional", R/"cross_sectional"/"crypto", R/"trend_following"):
    sys.path.insert(0, str(p))
import characterize as ch
from trend import tsmom_ensemble, realized_vol, portfolio_returns_directional
import sim_1000 as S

def sticky_signal(sig: pd.DataFrame, n: int) -> pd.DataFrame:
    if n <= 0: return sig
    out = sig.copy()
    for c in sig.columns:
        v = sig[c].values; held = np.zeros(len(v)); h = 0.0; dis = 0
        for i, x in enumerate(v):
            x = 0.0 if np.isnan(x) else x
            if h == 0.0 or np.sign(x) == np.sign(h):
                h, dis = x, 0                       # entry from flat, or same direction: follow
            else:
                dis += 1                            # disagreement: wait N days before exit/flip
                if dis > n: h, dis = x, 0
            held[i] = h
        out[c] = held
    return out

def met(s):
    s = s.dropna(); s = s[s.index >= s[s != 0].index[0]]
    eq = (1+s).cumprod(); z = (s-s.mean())/s.std(ddof=1)
    return s.mean()/s.std(ddof=1)*np.sqrt(365), (eq/eq.cummax()-1).min()*100

panel = S.panel
print("(a) FULL BOOK, PIT 62 coins, funding on, 4.4 bps (Sharpe is scale-free; DD at raw scale)")
price, fwd = panel["price"], panel["fwd_ret"]; accr = -panel["funding"].shift(-1)
sig = tsmom_ensemble(panel, lookbacks=ch.TSMOM_LOOKBACKS, vol_window=ch.VOL_WINDOW)
vol = realized_vol(price, vol_window=ch.VOL_WINDOW)
for n in (0, 1, 2):
    sg = sticky_signal(sig, n)
    p = portfolio_returns_directional(sg, fwd, costs_bps=4.4, accrual=accr, vol=vol,
                                      vol_target=ch.VOL_TARGET, leverage_cap=ch.LEVERAGE_CAP)
    held = ch.scale_positions(sg, fwd, vol, ch.VOL_TARGET, ch.LEVERAGE_CAP).fillna(0)
    turn = held.diff().abs().sum(axis=1).mean()*365
    sh, dd = met(p)
    flips = int((np.sign(sg).diff().abs() > 0).sum().sum())
    print(f"  exit delay {n}d: Sharpe {sh:+.3f}   turnover {turn:6.1f}x/yr   cost drag {turn*4.4e-4*100:5.2f}%/yr (raw scale)"
          f"   sign changes {flips}")

print("\n(b) REALISTIC $1000 MAJORS account (min position $12, min order $10, causal scale)")
MAJ = S.MAJ
def run_sticky(coins, vt, n):
    orig = S.tsmom_ensemble
    S.tsmom_ensemble = lambda sub, **kw: sticky_signal(orig(sub, **kw), n)
    try: return S.run(coins, vt)
    finally: S.tsmom_ensemble = orig
for vt in (0.10, 0.20):
    for n in (0, 1, 2):
        df = run_sticky(MAJ, vt, n); p = df.pnl
        eq = 1000 + p.cumsum(); dd = (eq - eq.cummax()).min()
        roll = p.rolling(365).sum().dropna()
        yrs = {y: g.pnl.sum() for y, g in df.groupby(df.index.year)}
        print(f"  vol {int(vt*100)}% exit delay {n}d: ${p.mean()*365:+4.0f}/yr  Sharpe {p.mean()/p.std(ddof=1)*np.sqrt(365):+.2f}  "
              f"maxDD ${dd:5.0f}  costs ${-df.cost.mean()*365:3.0f}/yr  1y negative {(roll<0).mean()*100:3.0f}%  "
              + " ".join(f"{y}:${v:+.0f}" for y, v in yrs.items()))
