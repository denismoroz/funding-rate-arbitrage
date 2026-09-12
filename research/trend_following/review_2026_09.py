"""Trend TSMOM re-examined under research/SCREENING.md (2026-09-12).

Old verdict died on correlation with XSMOM (+0.40) and PBO 0.63 (null on a
homogeneous menu). New rules: correlation is sizing, not a filter. So ask:
  1) is the net edge real on honest data — PIT panel, funding on, REAL HL cost
     4.4 bps (the committed run used 8.5), and is it decaying?
  2) executable on HL at our capital (leg >= $12)?
  3) tail by direct measurement (pumps / crashes / carry-bad weeks)
  4) profile (skew) — the owner prefers carry
  5) correlation with FRAB-proxy and XSMOM -> sizing only
"""
import sys, json
from pathlib import Path
import numpy as np, pandas as pd
R = Path("/Users/d/prj/funding-rate-arbitrage/research")
for p in (R, R/"validation_harness", R/"cross_sectional", R/"cross_sectional"/"crypto", R/"trend_following"):
    sys.path.insert(0, str(p))
import characterize as ch
from trend import tsmom_ensemble, realized_vol, portfolio_returns_directional
import xsec

REAL_COST = 4.4
panel = ch.build_pt_panel()
price, fwd, funding = panel["price"], panel["fwd_ret"], panel["funding"]
vol = realized_vol(price, vol_window=ch.VOL_WINDOW)
accr = -funding.shift(-1)
sig = tsmom_ensemble(panel, lookbacks=ch.TSMOM_LOOKBACKS, vol_window=ch.VOL_WINDOW)

def book(cost):
    return portfolio_returns_directional(sig, fwd, costs_bps=cost, accrual=accr, vol=vol,
                                         vol_target=ch.VOL_TARGET, leverage_cap=ch.LEVERAGE_CAP).dropna()
held = ch.scale_positions(sig, fwd, vol, ch.VOL_TARGET, ch.LEVERAGE_CAP)

def met(s):
    sh = s.mean()/s.std(ddof=1)*np.sqrt(365)
    eq = (1+s).cumprod(); dd = (eq/eq.cummax()-1).min()
    z = (s-s.mean())/s.std(ddof=1)
    return sh, s.mean()*365, s.std(ddof=1)*np.sqrt(365), dd, float((z**3).mean())

out = {}
print("\n=== FILTER 1: edge on honest data ===")
for lab, c in (("committed cost 8.5bps", 8.5), ("real HL cost 4.4bps", REAL_COST)):
    sh, ann, v, dd, sk = met(book(c))
    print(f"  {lab:<22} Sharpe {sh:+.2f}  (raw scale: ann {ann*100:+.0f}%, vol {v*100:.0f}%)")
b = book(REAL_COST)
sh_all = met(b)[0]
# rescale to a deployable 20% annual vol — Sharpe/skew are scale-invariant, DD is not
k = 0.20 / (b.std(ddof=1)*np.sqrt(365)); bs = b*k
print(f"\n  at a deployable 20%/yr vol (x{k:.2f} of raw book):")
sh, ann, v, dd, sk = met(bs)
print(f"    ann {ann*100:+.1f}%   maxDD {dd*100:.1f}%   Sharpe {sh:+.2f}   skew {sk:+.2f}")
out["real_cost"] = dict(sharpe=sh, ann20=ann, maxdd20=dd, skew=sk)
print("\n  by calendar year (20% vol scale):")
for y, g in bs.groupby(bs.index.year):
    s_, a_, _, d_, _ = met(g)
    print(f"    {y}: ret {((1+g).prod()-1)*100:+6.1f}%   Sharpe {s_:+.2f}   maxDD {d_*100:.1f}%")
end = bs.index[-1]
for m in (12, 6):
    g = bs[bs.index > end - pd.DateOffset(months=m)]
    print(f"    trailing {m}m: ret {((1+g).prod()-1)*100:+6.1f}%   Sharpe {met(g)[0]:+.2f}")
W = 365
roll = np.array([np.prod(1+bs.values[i:i+W])-1 for i in range(len(bs)-W+1)])
print(f"  rolling 1y windows: worst {roll.min()*100:+.1f}%  median {np.median(roll)*100:+.1f}%  "
      f"negative {(roll<0).mean()*100:.0f}%")
wk = (1+bs).groupby(pd.Grouper(freq="W")).prod()-1
srt = wk.sort_values(ascending=False)
print(f"  concentration: all weeks {((1+wk).prod()-1)*100:+.0f}%, without best 5 weeks "
      f"{((1+wk.drop(srt.index[:5])).prod()-1)*100:+.0f}%")

print("\n=== FILTER 2: executable on HL at our capital ===")
g = held.loc[b.index].abs()
n_pos = (g > 1e-6).sum(axis=1)
gross = g.sum(axis=1) * k
print(f"  simultaneous positions: median {int(n_pos.median())}, p90 {int(n_pos.quantile(.9))}")
print(f"  gross notional per $1 capital at 20% vol: median {gross.median():.2f}x, p90 {gross.quantile(.9):.2f}x")
per_leg = (g*k).where(g > 1e-6)
small = per_leg.stack().quantile(0.25)
print(f"  25th-pct leg size per $1 capital: {small:.4f}  ->  capital needed for a $12 leg: ${12/small:,.0f}")
out["min_capital_for_12usd_leg"] = 12/small

print("\n=== FILTER 3/4: tails and profile ===")
btc = price["BTC"].pct_change()
xw = (1+btc).groupby(pd.Grouper(freq="W")).prod()-1
carry = funding[[c for c in ("BTC","ETH","SOL") if c in funding.columns]].mean(axis=1).groupby(pd.Grouper(freq="W")).sum()
df = pd.concat([wk, carry, xw], axis=1).dropna(); df.columns = ["TREND","CARRY","BTC"]
for lab, sel in (("10 sharpest BTC pumps", df.nlargest(10,"BTC")),
                 ("10 sharpest BTC crashes", df.nsmallest(10,"BTC")),
                 ("10 worst carry weeks", df.nsmallest(10,"CARRY"))):
    print(f"  {lab:<24} TREND mean {sel.TREND.mean()*100:+6.2f}%   positive {(sel.TREND>0).sum()}/10")

print("\n=== FILTER 5: correlation -> sizing ===")
S = sum(xsec.zscore_cross_section(price.pct_change(l)) for l in (14,21,30,45,60))/5
Wt = pd.DataFrame(0.0, index=S.index, columns=S.columns)
for dt, row in S.iterrows():
    v = row.dropna()
    if len(v) < 16: continue
    o = v.sort_values(ascending=False); Wt.loc[dt, o.index[:8]] = 1/8; Wt.loc[dt, o.index[-8:]] = -1/8
xs = xsec.portfolio_returns(Wt, fwd, costs_bps=REAL_COST, rebal_every=7, accrual=accr)
xw_ = (1+xs).groupby(pd.Grouper(freq="W")).prod()-1
cc = pd.concat([wk.rename("TREND"), carry.rename("CARRY"), xw_.rename("XSMOM")], axis=1).dropna().corr()
print(f"  weekly corr TREND~CARRY(FRAB proxy) {cc.loc['TREND','CARRY']:+.2f}   TREND~XSMOM {cc.loc['TREND','XSMOM']:+.2f}")
out["corr"] = dict(carry=float(cc.loc['TREND','CARRY']), xsmom=float(cc.loc['TREND','XSMOM']))
json.dump(out, open(R/"trend_following"/"review_2026_09.json","w"), indent=1, default=float)

print("\n=== FILTER 2 variant: majors-only book (executable at small capital?) ===")
MAJORS = [c for c in ("BTC","ETH","SOL","XRP","BNB","DOGE","ADA","AVAX","LINK","LTC") if c in price.columns]
sub = {k: (v[MAJORS] if isinstance(v, pd.DataFrame) and set(MAJORS) <= set(v.columns) else v)
       for k, v in panel.items()}
sub["coins"] = MAJORS
sig_m = tsmom_ensemble(sub, lookbacks=ch.TSMOM_LOOKBACKS, vol_window=ch.VOL_WINDOW)
vol_m = realized_vol(sub["price"], vol_window=ch.VOL_WINDOW)
bm = portfolio_returns_directional(sig_m, sub["fwd_ret"], costs_bps=REAL_COST,
        accrual=-sub["funding"].shift(-1), vol=vol_m, vol_target=ch.VOL_TARGET,
        leverage_cap=ch.LEVERAGE_CAP).dropna()
km = 0.20 / (bm.std(ddof=1)*np.sqrt(365)); bms = bm*km
sh, ann, v, dd, sk = met(bms)
print(f"  {len(MAJORS)} majors {MAJORS}")
print(f"  at 20% vol: ann {ann*100:+.1f}%  maxDD {dd*100:.1f}%  Sharpe {sh:+.2f}  skew {sk:+.2f}")
for y, g in bms.groupby(bms.index.year):
    print(f"    {y}: ret {((1+g).prod()-1)*100:+6.1f}%   Sharpe {met(g)[0]:+.2f}")
roll = np.array([np.prod(1+bms.values[i:i+365])-1 for i in range(len(bms)-365+1)])
print(f"  rolling 1y: worst {roll.min()*100:+.1f}%  negative {(roll<0).mean()*100:.0f}%")
hm = ch.scale_positions(sig_m, sub["fwd_ret"], vol_m, ch.VOL_TARGET, ch.LEVERAGE_CAP).loc[bm.index].abs()*km
legs = hm.where(hm > 1e-6).stack()
print(f"  positions median {int((hm>1e-6).sum(axis=1).median())}, 25th-pct leg per $1 {legs.quantile(.25):.4f}"
      f" -> capital for $12 leg ${12/legs.quantile(.25):,.0f}")
print("  CAVEAT: majors picked by today's list = survivor pick, but none of these died;"
      " the bias is small here, not zero.")
out["majors"] = dict(sharpe=sh, ann20=ann, maxdd20=dd, min_capital=12/legs.quantile(.25))
json.dump(out, open(R/"trend_following"/"review_2026_09.json","w"), indent=1, default=float)
