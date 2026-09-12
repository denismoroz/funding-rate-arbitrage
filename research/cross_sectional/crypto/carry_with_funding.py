"""Crypto cross-sectional CARRY judged WITH its funding income (2026-09-13).

The menu run in crypto_pkg scored carry on price only (accrual never passed), which
is meaningless for carry: its income IS funding. Result, PIT universe, weekly, 4.4bps:
  price only  -22.6%/yr Sharpe -0.78  |  with funding +4.1%/yr Sharpe +0.14, maxDD -45%
Funding alone is +26.7%/yr, but shorting expensive-to-carry coins means shorting what
rallies: price risk eats it. Still dead — now for the right reason. FRAB collects the
same funding delta-neutrally, which is why it works and this does not.
"""
import json, numpy as np, pandas as pd, cryptodata, signals, xsec
coins = json.loads(open("universe_pit.json").read())["coins"]
P = cryptodata.load_panel(coins=coins)
w = xsec.rank_to_weights(xsec.zscore_cross_section(signals.carry(P, smooth_days=14)), tercile_frac=1/3)
for lab, acc in (("price only", xsec.NO_ACCRUAL), ("with funding", -P["funding"].shift(-1))):
    s = xsec.portfolio_returns(w, P["fwd_ret"], costs_bps=4.4, rebal_every=7, accrual=acc).dropna()
    eq = (1+s).cumprod()
    print(f"{lab:<14} ann {s.mean()*365*100:+.1f}%  Sharpe {s.mean()/s.std(ddof=1)*np.sqrt(365):+.2f}  "
          f"maxDD {(eq/eq.cummax()-1).min()*100:.1f}%")
