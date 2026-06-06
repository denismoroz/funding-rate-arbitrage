"""
Walk-forward probe: does parameter tuning on crypto TREND survive out-of-sample, or is the
seductive in-sample grid number (30-45% CAGR) just curve-fitting noise?

Method (honest, live-replicable):
  * Candidate params: MA crossovers {10/50,20/100,50/200} + TSMOM {30,60,90}, long/flat, BTC/ETH/SOL basket.
  * Precompute each param's net daily return series (5bps).
  * Every REBAL days: pick the param with the best trailing-IS_WINDOW Sharpe (uses ONLY past data),
    trade it for the next REBAL days. Stitch those OOS chunks -> a real "tuned-live" equity curve.
  * Compare against: (a) in-sample BEST param applied throughout (the overfit fantasy number),
    (b) the a-priori default 50/200, (c) buy & hold BTC, (d) average-of-all-params.
"""
import sys, itertools
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q

COINS = ["BTC", "ETH", "SOL"]
COST = 5.0
MA = [(10, 50), (20, 100), (50, 200)]
MOM = [30, 60, 90]


def basket_weight_ma(px, f, s):
    sig = (px.rolling(f).mean() > px.rolling(s).mean()).astype(float)
    return sig / px.shape[1]           # per-coin weight, equal-weight long/flat basket


def basket_weight_mom(px, k):
    sig = (px / px.shift(k) - 1.0 > 0).astype(float)
    return sig / px.shape[1]


def main():
    px = q.load_closes(COINS, "1d").dropna()
    params, rets = [], {}
    for f, s in MA:
        name = f"ma{f}_{s}"
        params.append(name)
        rets[name] = q.backtest_weights(px, basket_weight_ma(px, f, s), cost_bps=COST)["ret_net"]
    for k in MOM:
        name = f"mom{k}"
        params.append(name)
        rets[name] = q.backtest_weights(px, basket_weight_mom(px, k), cost_bps=COST)["ret_net"]
    R = pd.DataFrame(rets).dropna()

    ann = 365
    def cagr(r): m = q.metrics_from_returns(r, "1d"); return m.get("cagr"), m.get("sharpe"), m.get("max_drawdown")

    # (a) in-sample BEST (full-sample champion) — the overfit number
    sharpes = {p: q.metrics_from_returns(R[p], "1d")["sharpe"] for p in params}
    best_full = max(sharpes, key=sharpes.get)
    cagrs = {p: q.metrics_from_returns(R[p], "1d")["cagr"] for p in params}
    best_full_cagr = max(cagrs, key=cagrs.get)

    # (e) WALK-FORWARD: pick best trailing-Sharpe param every REBAL days
    IS_WINDOW, REBAL = 252, 21
    chosen = pd.Series(index=R.index, dtype=object)
    wf = pd.Series(0.0, index=R.index)
    i = IS_WINDOW
    while i < len(R):
        win = R.iloc[i - IS_WINDOW:i]
        sh = win.mean() / win.std()                       # trailing Sharpe per param (past only)
        pick = sh.idxmax()
        j = min(i + REBAL, len(R))
        seg = R[pick].iloc[i:j]
        wf.iloc[i:j] = seg.values
        chosen.iloc[i:j] = pick
        i = j
    wf = wf.iloc[IS_WINDOW:]

    btc = q.backtest_weights(px["BTC"], pd.Series(1.0, index=px.index), cost_bps=0)["ret_net"]

    print("CANDIDATE PARAMS — full-sample (in-sample) numbers:")
    for p in params:
        c, s, d = cagr(R[p]); print(f"  {p:10s} CAGR {c*100:6.1f}%  Sharpe {s:5.2f}  MDD {d*100:6.1f}%")
    print(f"\n(a) in-sample BEST by CAGR   = {best_full_cagr}  -> CAGR {cagrs[best_full_cagr]*100:.1f}%  [the fantasy]")
    print(f"(a) in-sample BEST by Sharpe = {best_full} -> Sharpe {sharpes[best_full]:.2f}")
    c, s, d = cagr(R['ma50_200']); print(f"(b) a-priori default ma50_200 CAGR {c*100:.1f}% Sharpe {s:.2f}")
    c, s, d = cagr(R.mean(axis=1)); print(f"(d) average-of-all-params     CAGR {c*100:.1f}% Sharpe {s:.2f}")
    cb, sb, db = cagr(btc.loc[wf.index]); print(f"(c) buy&hold BTC (same window) CAGR {cb*100:.1f}% Sharpe {sb:.2f}")
    print("\n>>> (e) WALK-FORWARD tuned-live (pick best trailing-Sharpe param, OOS):")
    cw, sw, dw = cagr(wf)
    print(f"    CAGR {cw*100:.1f}%  Sharpe {sw:.2f}  MDD {dw*100:.1f}%   (window {wf.index[0].date()}..{wf.index[-1].date()})")
    print(f"    param switches: {(chosen.dropna() != chosen.dropna().shift()).sum()}  | param usage:")
    print("   ", chosen.dropna().value_counts().to_dict())
    print("\nINTERPRETATION: compare (e) WFO-live vs (a) in-sample-best. If (e) << (a), tuning is fitting noise.")


if __name__ == "__main__":
    main()
