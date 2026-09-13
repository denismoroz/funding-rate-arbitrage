"""Sticky exit applied to XSMOM (2026-09-13).
XSMOM rebalances weekly, so there is no hourly flicker; the analogue of a sticky exit is
not dropping a coin the first week it leaves the top/bottom 8. Pre-registered variants:
  BASE8     current: longs = top 8, shorts = bottom 8 by the momentum ensemble
  GRACE1W   an incumbent that falls out is kept ONE more rebalance; dropped if still out
  BUFFER12  an incumbent is kept while its rank stays within top/bottom 12
New names fill free slots from the best ranks; a leg never exceeds 8 (best-ranked kept).
Full history: PIT universe, funding on, 4.4 bps. Forward: live prod prices Jun-Sep 2026."""
import sys, json, sqlite3
from pathlib import Path
import numpy as np, pandas as pd
R = Path(__file__).resolve().parents[2]
for p in (R, R/"cross_sectional", R/"cross_sectional"/"crypto", R/"validation_harness"):
    sys.path.insert(0, str(p))
import cryptodata, signals, xsec, crypto_pkg

K, REB, COST = 8, 7, 4.4

def book_weights(score: pd.DataFrame, mode: str) -> pd.DataFrame:
    W = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    legs = {"L": [], "S": []}; out = {"L": {}, "S": {}}
    last = None
    for i, (t, row) in enumerate(score.iterrows()):
        if i % REB != 0:
            if last is not None: W.loc[t] = last
            continue
        v = row.dropna()
        if len(v) < 2*K:
            continue
        order = v.sort_values(ascending=False)
        ranks = {"L": list(order.index), "S": list(order.index[::-1])}
        new = {}
        for leg in ("L", "S"):
            top = ranks[leg][:K]; pos = {c: j for j, c in enumerate(ranks[leg])}
            keep = []
            for c in legs[leg]:
                if c not in pos: continue
                if mode == "BASE8":
                    ok = pos[c] < K
                elif mode == "GRACE1W":
                    if pos[c] < K: out[leg][c] = 0; ok = True
                    else:
                        out[leg][c] = out[leg].get(c, 0) + 1; ok = out[leg][c] <= 1
                elif mode == "BUFFER12":
                    ok = pos[c] < 12
                if ok: keep.append(c)
            chosen = sorted(set(keep), key=lambda c: pos[c])[:K]
            for c in top:
                if len(chosen) >= K: break
                if c not in chosen: chosen.append(c)
            new[leg] = chosen
        clash = set(new["L"]) & set(new["S"])
        if clash:
            new["L"] = [c for c in new["L"] if c not in clash]; new["S"] = [c for c in new["S"] if c not in clash]
        for leg in ("L", "S"):
            for c in list(out[leg]):
                if c not in new[leg]: out[leg].pop(c, None)
        legs = new
        w = pd.Series(0.0, index=score.columns)
        if new["L"]: w[new["L"]] = 1.0/len(new["L"])
        if new["S"]: w[new["S"]] = -1.0/len(new["S"])
        W.loc[t] = w; last = w
    return W

def stats(p):
    p = p.dropna(); p = p[p.index >= p[p != 0].index[0]]
    eq = (1+p).cumprod()
    wk = (1+p).groupby(pd.Grouper(freq="W")).prod()-1
    srt = wk.sort_values(ascending=False)
    roll = np.array([np.prod(1+p.values[i:i+365])-1 for i in range(len(p)-364)]) if len(p) > 365 else np.array([np.nan])
    return dict(ann=p.mean()*365*100, sharpe=p.mean()/p.std(ddof=1)*np.sqrt(365), dd=(eq/eq.cummax()-1).min()*100,
                neg1y=np.nanmean(roll < 0)*100, no_top5=((1+wk.drop(srt.index[:5])).prod()-1)*100,
                years={y: ((1+g).prod()-1)*100 for y, g in p.groupby(p.index.year)})

def turnover(W):
    rb = W.iloc[::REB]
    return rb.diff().abs().sum(axis=1).mean() * 52

if __name__ == "__main__":
    P = cryptodata.load_panel(coins=crypto_pkg._frozen_universe())
    score = signals.momentum_ensemble(P, lookbacks=(14, 21, 30, 45, 60))
    accr = -P["funding"].shift(-1)
    print("FULL HISTORY — PIT universe, funding on, 4.4 bps, weekly")
    print(f"{'variant':<10}{'ann':>8}{'Sharpe':>8}{'maxDD':>8}{'turnover/yr':>13}{'cost/yr':>9}{'1y neg':>8}{'no top5 wk':>12}   by year")
    for mode in ("BASE8", "GRACE1W", "BUFFER12"):
        W = book_weights(score, mode)
        p = xsec.portfolio_returns(W, P["fwd_ret"], costs_bps=COST, rebal_every=REB, accrual=accr)
        s = stats(p); tv = turnover(W)
        print(f"{mode:<10}{s['ann']:>+7.1f}%{s['sharpe']:>+8.2f}{s['dd']:>7.1f}%{tv:>12.1f}x{tv*COST/1e4*100:>8.2f}%"
              f"{s['neg1y']:>7.0f}%{s['no_top5']:>+11.0f}%   " + " ".join(f"{y}:{v:+.0f}%" for y, v in s['years'].items()))

    # forward: live prod daily prices, live universe, no funding (same for all variants)
    db = sqlite3.connect("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/xsmom_audit.db")
    uni = json.loads(db.execute("SELECT params_json FROM strategies WHERE id=2").fetchone()[0])["universe"]
    px = pd.read_sql("SELECT coin, day_ms, close FROM xsmom_daily_prices", db)
    px["d"] = pd.to_datetime(px.day_ms // 86400000 * 86400000, unit="ms")
    price = px.pivot_table(index="d", columns="coin", values="close").sort_index()
    price = price[[c for c in uni if c in price.columns]]
    FP = {"price": price, "fwd_ret": price.pct_change().shift(-1)}
    fscore = signals.momentum_ensemble(FP, lookbacks=(14, 21, 30, 45, 60))
    start = pd.Timestamp("2026-06-11")          # first Thursday on/after live start
    fscore = fscore[fscore.index >= start]; ffwd = FP["fwd_ret"].reindex(fscore.index)
    print("\nFORWARD — live prod prices 2026-06-11..09-12, live 32-coin universe, costs, NO funding")
    for mode in ("BASE8", "GRACE1W", "BUFFER12"):
        W = book_weights(fscore, mode)
        p = xsec.portfolio_returns(W, ffwd, costs_bps=COST, rebal_every=REB, accrual=xsec.NO_ACCRUAL).dropna()
        eq = (1+p).cumprod()
        print(f"  {mode:<10} total {((eq.iloc[-1])-1)*100:+6.2f}%   maxDD {(eq/eq.cummax()-1).min()*100:6.2f}%   "
              f"turnover {turnover(W):5.1f}x/yr")
