"""Where does the Sharpe uplift of the $1000 sim come from?
A) frictionless, full-sample scale  (the published 0.77)
B) frictionless, CAUSAL portfolio vol scale      -> effect of vol timing alone
C) $1000 frictions, causal scale (the sim)       -> adds min-size + order band
Same start date for all three so the sample is identical."""
import sys
from pathlib import Path
import numpy as np, pandas as pd
R = Path("/Users/d/prj/funding-rate-arbitrage/research")
for p in (R, R/"validation_harness", R/"cross_sectional", R/"cross_sectional"/"crypto", R/"trend_following"):
    sys.path.insert(0, str(p))
import characterize as ch
from trend import tsmom_ensemble, realized_vol
import sim_1000 as S

def sh(x): x = x.dropna(); return x.mean()/x.std(ddof=1)*np.sqrt(365)
panel = S.panel
for lab, coins in (("FULL", list(panel["coins"])), ("MAJORS", S.MAJ)):
    price, fwd = panel["price"][coins], panel["fwd_ret"][coins]
    accr = (-panel["funding"].shift(-1))[coins].reindex_like(fwd).fillna(0.0)
    sub = {**panel, "price": price, "fwd_ret": fwd, "funding": panel["funding"][coins], "coins": coins}
    sig = tsmom_ensemble(sub, lookbacks=ch.TSMOM_LOOKBACKS, vol_window=ch.VOL_WINDOW)
    raw = ch.scale_positions(sig, fwd, realized_vol(price, vol_window=ch.VOL_WINDOW), ch.VOL_TARGET, ch.LEVERAGE_CAP).fillna(0.0)
    turn = raw.diff().abs().sum(axis=1)
    A = (raw*fwd.fillna(0)).sum(axis=1) + (raw*accr).sum(axis=1) - turn*S.COST
    trail = A.where(A != 0).rolling(180, min_periods=90).std(ddof=1).shift(1)*np.sqrt(365)
    k = (0.20/trail).clip(upper=5.0)
    heldB = raw.mul(k, axis=0).fillna(0.0)
    B = (heldB*fwd.fillna(0)).sum(axis=1) + (heldB*accr).sum(axis=1) - heldB.diff().abs().sum(axis=1)*S.COST
    start = k.dropna().index[0]
    for vt in (0.20, 0.30):
        C = S.run(coins, vt).pnl
        print(f"{lab:<7} vt={int(vt*100)}%  A frictionless full-sample {sh(A[A.index>=start]):+.2f} | "
              f"B frictionless CAUSAL {sh(B[B.index>=start]):+.2f} | C $1000 frictions {sh(C):+.2f}")
