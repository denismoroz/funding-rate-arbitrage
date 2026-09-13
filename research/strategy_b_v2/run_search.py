"""Run the pre-registered 144-config search (see harness.py docstring for the rules)."""
import numpy as np, pandas as pd
from scipy.stats import spearmanr
import harness as H

data = {"r": H.load("research"), "f": H.load("forward")}
carry_daily = {k: {c: pd.Series(np.cumsum(H.carry_pnl(d[c])), index=d[c].index).resample("D").last()
                   for c in H.COINS} for k, d in data.items()}
rows = []
seen = {}
for cfg in H.configs():
    key = (cfg["sticky"], cfg["a"], cfg["thr"])
    if key not in seen:
        seen[key] = {k: H.coin_equity(d, cfg["sticky"], cfg["a"], cfg["thr"], carry=False) for k, d in data.items()}
        print(f"  sims done sticky={key[0]} a={key[1]} thr={key[2]}", flush=True)
    out = dict(cfg)
    for k in ("r", "f"):
        eq = seen[key][k]
        if cfg["carry"]:
            eq = eq.add(pd.DataFrame(carry_daily[k]).reindex(eq.index).ffill().fillna(0.0), fill_value=0.0)
        rp = H.portfolio(eq, cfg["weights"])
        if k == "r":
            for lab, (lo, hi) in (("train", H.TRAIN), ("test", H.TEST)):
                for m, v in H.seg(rp, lo, hi).items(): out[f"{lab}_{m}"] = v
        else:
            for m, v in H.seg(rp, *H.FWD).items(): out[f"fwd_{m}"] = v
    rows.append(out)
T = pd.DataFrame(rows)
T.to_csv("search_results.csv", index=False)
is_cur = np.all([T[k] == v for k, v in H.CURRENT.items()], axis=0)
cur = T[is_cur].iloc[0]
best = T.sort_values("train_calmar", ascending=False).iloc[0]
cols = ["sticky", "a", "thr", "carry", "weights"]
def show(lab, r):
    print(f"{lab:<10} {dict((c, r[c]) for c in cols)}")
    print(f"           TRAIN ann {r.train_ann:+6.1f}% DD {r.train_dd:5.1f}% Calmar {r.train_calmar:5.2f} | "
          f"TEST ret {r.test_ret:+6.1f}% DD {r.test_dd:5.1f}% | FWD ret {r.fwd_ret:+6.2f}% DD {r.fwd_dd:5.1f}%")
print()
show("CURRENT", cur); show("SELECTED", best)
rank_test = (T.test_ret > best.test_ret).sum() + 1
rho = spearmanr(T.train_calmar, T.test_calmar).correlation
print(f"\nselected config ranks #{rank_test}/144 on TEST return; Spearman(train Calmar, test Calmar) = {rho:+.2f}")
print("\nSUCCESS CHECK (pre-registered):")
checks = {
    "TEST return > 0": best.test_ret > 0, "FORWARD return > 0": best.fwd_ret > 0,
    "TEST maxDD <= 15%": best.test_dd <= 15, "FORWARD maxDD <= 15%": best.fwd_dd <= 15,
    "beats CURRENT on TEST": best.test_ret > cur.test_ret, "beats CURRENT on FORWARD": best.fwd_ret > cur.fwd_ret,
}
for k, v in checks.items(): print(f"  {'PASS' if v else 'FAIL'}  {k}")
print("\ntop 10 by TRAIN Calmar, with what they did afterwards:")
print(T.sort_values("train_calmar", ascending=False).head(10)[cols + ["train_calmar", "test_ret", "test_dd", "fwd_ret", "fwd_dd"]]
      .round(2).to_string(index=False))
print("\nhow many of 144 configs are positive: TEST", int((T.test_ret > 0).sum()), " FORWARD", int((T.fwd_ret > 0).sum()),
      " both", int(((T.test_ret > 0) & (T.fwd_ret > 0)).sum()))
