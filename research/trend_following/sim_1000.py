"""Trend TSMOM on a REAL $1000 account — dollars, not percentages of a frictionless book.

Frictions a backtest on fractions ignores but a small account cannot:
  * minimum position: a target under $12 cannot be held -> flat;
  * minimum order: HL rejects orders under ~$10 -> a rebalance smaller than $10
    is skipped and the old position is kept (a no-trade band);
  * real cost 4.4 bps per leg, funding paid/received on every held day.
Capital stays at $1000 (no compounding) so every number reads as dollars per year.
"""
import sys, json
from pathlib import Path
import numpy as np, pandas as pd
R = Path("/Users/d/prj/funding-rate-arbitrage/research")
for p in (R, R/"validation_harness", R/"cross_sectional", R/"cross_sectional"/"crypto", R/"trend_following"):
    sys.path.insert(0, str(p))
import characterize as ch
from trend import tsmom_ensemble, realized_vol

CAP, MIN_POS, MIN_ORDER, COST = 1000.0, 12.0, 10.0, 4.4e-4
panel = ch.build_pt_panel()

def run(coins, vol_target_ann):
    price = panel["price"][coins]; fwd = panel["fwd_ret"][coins]
    accr = (-panel["funding"].shift(-1))[coins].reindex_like(fwd).fillna(0.0)
    sub = {**panel, "price": price, "fwd_ret": fwd, "funding": panel["funding"][coins], "coins": coins}
    sig = tsmom_ensemble(sub, lookbacks=ch.TSMOM_LOOKBACKS, vol_window=ch.VOL_WINDOW)
    vol = realized_vol(price, vol_window=ch.VOL_WINDOW)
    raw = ch.scale_positions(sig, fwd, vol, ch.VOL_TARGET, ch.LEVERAGE_CAP).fillna(0.0)
    raw_pnl = (raw * fwd.fillna(0)).sum(axis=1)
    # CAUSAL scale: trailing 180d realized vol of the raw book, known at t (shift 1).
    # A full-sample scale would peek into the future AND decide which positions clear
    # the $12 minimum, so it would bias the small-account result, not just its size.
    trail = raw_pnl.where(raw_pnl != 0).rolling(180, min_periods=90).std(ddof=1).shift(1) * np.sqrt(365)
    k = (vol_target_ann / trail).clip(upper=5.0)
    target = raw.mul(k, axis=0).fillna(0.0) * CAP
    target = target.where(target.abs() >= MIN_POS, 0.0)
    cur = pd.Series(0.0, index=coins)
    rows = []
    for t in target.index:
        tg = target.loc[t]
        delta = tg - cur
        trade = delta.where((delta.abs() >= MIN_ORDER) | (tg == 0), 0.0)
        cur = cur + trade
        r = fwd.loc[t].fillna(0.0)
        rows.append((t, float((cur * r).sum()), float((cur * accr.loc[t]).sum()),
                     float(trade.abs().sum() * COST), int((cur != 0).sum()), float(cur.abs().sum())))
    df = pd.DataFrame(rows, columns=["t", "price", "funding", "cost", "npos", "gross"]).set_index("t")
    df = df[df.index >= k.dropna().index[0]]      # start once the causal scale exists
    df["pnl"] = df.price + df.funding - df.cost
    return df

def report(lab, df):
    p = df.pnl
    eq = CAP + p.cumsum(); dd = (eq - eq.cummax()).min()
    sh = p.mean() / p.std(ddof=1) * np.sqrt(365)
    roll = p.rolling(365).sum().dropna()
    yrs = {y: g.pnl.sum() for y, g in df.groupby(df.index.year)}
    print(f"\n{lab}")
    print(f"  realized vol {p.std(ddof=1)*np.sqrt(365)/CAP*100:.1f}%/yr of $1000")
    print(f"  per year: ${p.mean()*365:+.0f}   Sharpe {sh:+.2f}   max drawdown ${dd:.0f}   "
          f"worst 1y ${roll.min():+.0f}   1y windows negative {(roll<0).mean()*100:.0f}%")
    print(f"  per year breakdown: price ${df.price.mean()*365:+.0f}  funding ${df.funding.mean()*365:+.0f}  "
          f"costs ${-df.cost.mean()*365:.0f}")
    print(f"  positions held: median {int(df.npos.median())}   gross notional median ${df.gross.median():.0f}")
    print("  by calendar year: " + "  ".join(f"{y}: ${v:+.0f}" for y, v in yrs.items()))
    return dict(per_year=p.mean()*365, sharpe=sh, maxdd=dd, worst1y=roll.min(),
                neg1y=(roll < 0).mean(), years={int(k): v for k, v in yrs.items()},
                npos=int(df.npos.median()), gross=df.gross.median(),
                funding=df.funding.mean()*365, costs=df.cost.mean()*365)

ALL = list(panel["coins"])
MAJ = [c for c in ("BTC","ETH","SOL","XRP","BNB","DOGE","ADA","AVAX","LINK","LTC") if c in ALL]
out = {}
for vt in (0.10, 0.20, 0.30):
    out[f"full_{int(vt*100)}"] = report(f"FULL book ({len(ALL)} coins), target vol {int(vt*100)}%", run(ALL, vt))
for vt in (0.10, 0.20, 0.30):
    out[f"majors_{int(vt*100)}"] = report(f"MAJORS ({len(MAJ)}), target vol {int(vt*100)}%", run(MAJ, vt))
json.dump(out, open(R/"trend_following"/"sim_1000.json", "w"), indent=1, default=float)
