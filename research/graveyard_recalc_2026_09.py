"""Graveyard re-scored under research/SCREENING.md defaults (2026-09-13).

Each old verdict is re-run twice on the same code path:
  OLD  = the settings the verdict was actually made on
  NEW  = point-in-time universe (with dead coins), funding ON, real HL cost 4.4 bps
Only the data/cost defaults change; signals and construction are the originals.
Why this matters: of the three default fixes only survivorship pushes old results
UP. Missing funding and 8.5 bps costs push them DOWN, so an old NO-GO is not
automatically safe (crypto carry: -22.6% price-only -> +4.1% with funding).
"""
import sys, json
from pathlib import Path
import numpy as np, pandas as pd
R = Path(__file__).resolve().parent
for p in (R, R/"cross_sectional", R/"cross_sectional"/"crypto", R/"validation_harness",
          R/"onchain_fundamental"):
    sys.path.insert(0, str(p))
import cryptodata, signals, xsec, value_signals as vs, mr_signals as mrs, crypto_pkg

SURV = crypto_pkg._frozen_universe(survivors_only=True)
PIT = crypto_pkg._frozen_universe()
PS, PP = cryptodata.load_panel(coins=SURV), cryptodata.load_panel(coins=PIT)

def met(s):
    s = s.dropna(); s = s[s.index >= s[s != 0].index[0]]
    sh = s.mean()/s.std(ddof=1)*np.sqrt(365)
    eq = (1+s).cumprod()
    return dict(ann=s.mean()*365*100, sharpe=sh, dd=(eq/eq.cummax()-1).min()*100)

def xsec_book(P, score, cost, funding, rebal=7):
    w = xsec.rank_to_weights(score, tercile_frac=1/3)
    acc = -P["funding"].shift(-1) if funding else xsec.NO_ACCRUAL
    return xsec.portfolio_returns(w, P["fwd_ret"], costs_bps=cost, rebal_every=rebal, accrual=acc)

def mr_book(P, cost, funding, beta_neutral):
    z = mrs.ts_mr_signal(P, 10)
    w = mrs.beta_neutral_weights(z, 2.0) if beta_neutral else mrs.normalize_weights(z, 2.0)
    acc = -P["funding"].shift(-1) if funding else xsec.NO_ACCRUAL
    return xsec.portfolio_returns(w, P["fwd_ret"], costs_bps=cost, rebal_every=1, accrual=acc)

rows = []
def add(name, old_fn, new_fn, old_desc):
    o, n = met(old_fn()), met(new_fn())
    rows.append((name, old_desc, o, n))
    print(f"{name:<30} OLD[{old_desc}] ann {o['ann']:+6.1f}% Sh {o['sharpe']:+.2f} DD {o['dd']:6.1f}%   "
          f"NEW ann {n['ann']:+6.1f}% Sh {n['sharpe']:+.2f} DD {n['dd']:6.1f}%")

old = "survivors, 8.5bp, funding"
for nm, fn in (("value dd90", lambda P: vs.drawdown_from_high(P, window=90)),
               ("value dd180", lambda P: vs.drawdown_from_high(P, window=180)),
               ("value dist_from_ma100", lambda P: vs.dist_from_ma(P, ma_window=100)),
               ("long-term reversal 120d", lambda P: vs.long_term_reversal(P, lookback=120))):
    add(nm, lambda fn=fn: xsec_book(PS, fn(PS), 8.5, True), lambda fn=fn: xsec_book(PP, fn(PP), 4.4, True), old)
add("reversal 7d (cross-section)",
    lambda: xsec_book(PS, xsec.zscore_cross_section(signals.reversal(PS, 7)), 8.5, True),
    lambda: xsec_book(PP, xsec.zscore_cross_section(signals.reversal(PP, 7)), 4.4, True), old)
add("TS mean-reversion raw (daily)",
    lambda: mr_book(PS, 8.5, True, False), lambda: mr_book(PP, 4.4, True, False), old + ", daily")
add("TS mean-reversion beta-neutral",
    lambda: mr_book(PS, 8.5, True, True), lambda: mr_book(PP, 4.4, True, True), old + ", daily")
add("crypto carry (x-section)",
    lambda: xsec_book(PS, xsec.zscore_cross_section(signals.carry(PS, 14)), 8.5, False),
    lambda: xsec_book(PP, xsec.zscore_cross_section(signals.carry(PP, 14)), 4.4, True),
    "survivors, 8.5bp, NO funding")
ens = lambda P: signals.momentum_ensemble(P, lookbacks=(14, 21, 30, 45, 60))
add("REFERENCE: XSMOM momentum", lambda: xsec_book(PS, ens(PS), 8.5, True),
    lambda: xsec_book(PP, ens(PP), 4.4, True), old)

# on-chain fee growth: its verdict was made with costs 4.4 but NO funding
try:
    from onchain_pkg import OnchainFundamentalPackage
    from fees_signal import fee_growth_ensemble, zscore_by_group
    from fees_data import DEFI_COINS, CHAIN_COINS
    pkg = OnchainFundamentalPackage(); pkg._build_menu()
    cc = pkg._common_coins
    Pc = cryptodata.load_panel(coins=cc)
    z = zscore_by_group(fee_growth_ensemble(pkg._fee_sub), defi_coins=[c for c in cc if c in DEFI_COINS],
                        chain_coins=[c for c in cc if c in CHAIN_COINS]).reindex(Pc["fwd_ret"].index)
    def oc(funding):
        w = xsec.rank_to_weights(z)
        acc = -Pc["funding"].shift(-1) if funding else xsec.NO_ACCRUAL
        return xsec.portfolio_returns(w, Pc["fwd_ret"], costs_bps=4.4, rebal_every=7, accrual=acc)
    add("on-chain fee growth", lambda: oc(False), lambda: oc(True), "4.4bp, NO funding")
except Exception as e:
    print(f"on-chain skipped: {type(e).__name__}: {e}")

json.dump([dict(name=a, old_settings=b, old=c, new=d) for a, b, c, d in rows],
          open(R/"graveyard_recalc_2026_09.json", "w"), indent=1, default=float)
