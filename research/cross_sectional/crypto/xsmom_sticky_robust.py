"""Is GRACE1W real, or a lucky path? (2026-09-13)
1) all 7 possible weekly rebalance anchors (a single anchor is one path);
2) neighbours: GRACE 2 weeks, BUFFER 10 and 16.
Weights are forward-filled from each rebalance day and charged daily (changes happen only
on rebalance days), so the anchor can be shifted without touching the pnl engine."""
import numpy as np, pandas as pd
import xsmom_sticky as X
import cryptodata, signals, xsec, crypto_pkg

def book_weights(score, mode, offset):
    K = X.K
    W = pd.DataFrame(np.nan, index=score.index, columns=score.columns)
    legs = {"L": [], "S": []}; out = {"L": {}, "S": {}}
    grace = {"GRACE1W": 1, "GRACE2W": 2}.get(mode, 0)
    buf = {"BUFFER10": 10, "BUFFER12": 12, "BUFFER16": 16}.get(mode, 0)
    for i, (t, row) in enumerate(score.iterrows()):
        if (i - offset) % X.REB != 0: continue
        v = row.dropna()
        if len(v) < 2*K: continue
        order = v.sort_values(ascending=False)
        ranks = {"L": list(order.index), "S": list(order.index[::-1])}
        new = {}
        for leg in ("L", "S"):
            pos = {c: j for j, c in enumerate(ranks[leg])}; keep = []
            for c in legs[leg]:
                if c not in pos: continue
                if grace:
                    if pos[c] < K: out[leg][c] = 0; ok = True
                    else: out[leg][c] = out[leg].get(c, 0) + 1; ok = out[leg][c] <= grace
                elif buf: ok = pos[c] < buf
                else: ok = pos[c] < K
                if ok: keep.append(c)
            chosen = sorted(set(keep), key=lambda c: pos[c])[:K]
            for c in ranks[leg][:K]:
                if len(chosen) >= K: break
                if c not in chosen: chosen.append(c)
            new[leg] = chosen
        clash = set(new["L"]) & set(new["S"])
        new = {k: [c for c in v_ if c not in clash] for k, v_ in new.items()}
        for leg in ("L", "S"):
            for c in list(out[leg]):
                if c not in new[leg]: out[leg].pop(c, None)
        legs = new
        w = pd.Series(0.0, index=score.columns)
        if new["L"]: w[new["L"]] = 1/len(new["L"])
        if new["S"]: w[new["S"]] = -1/len(new["S"])
        W.loc[t] = w
    return W.ffill().fillna(0.0)

P = cryptodata.load_panel(coins=crypto_pkg._frozen_universe())
score = signals.momentum_ensemble(P, lookbacks=(14, 21, 30, 45, 60))
accr = -P["funding"].shift(-1)
MODES = ["BASE8", "GRACE1W", "GRACE2W", "BUFFER10", "BUFFER12", "BUFFER16"]
res = {m: [] for m in MODES}
for off in range(7):
    for m in MODES:
        W = book_weights(score, m, off)
        p = xsec.portfolio_returns(W, P["fwd_ret"], costs_bps=X.COST, rebal_every=1, accrual=accr)
        s = X.stats(p); res[m].append((s["sharpe"], s["dd"], s["neg1y"], s["no_top5"]))
    print(f"  anchor {off} done", flush=True)
print(f"\n{'variant':<10}" + "".join(f"{'anchor '+str(o):>11}" for o in range(7)) + f"{'median':>9}{'beats BASE8':>13}")
base = [r[0] for r in res["BASE8"]]
for m in MODES:
    sh = [r[0] for r in res[m]]
    beats = sum(a > b for a, b in zip(sh, base)) if m != "BASE8" else "-"
    print(f"{m:<10}" + "".join(f"{x:>+11.2f}" for x in sh) + f"{np.median(sh):>+9.2f}{str(beats):>13}")
print("\nmedian across anchors: maxDD / 1y-negative / return without best 5 weeks")
for m in MODES:
    a = np.array(res[m])
    print(f"  {m:<10} DD {np.median(a[:,1]):6.1f}%   neg1y {np.median(a[:,2]):4.0f}%   no-top5 {np.median(a[:,3]):+6.0f}%")
