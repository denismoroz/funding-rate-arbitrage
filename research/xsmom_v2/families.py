"""Post-verdict diagnostics (do NOT change the NO-GO): are the structural levers robust as FAMILIES on the
prod-like universe (>= 547 listed days), in discovery AND holdout? Also isolates each lever on top of the prod config."""
import itertools, json, sys
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine as E
import search as S

p = E.build_panel(min_days=547)
E._resid_cache.clear()
keys = list(S.GRID)
configs = [dict(zip(keys, v)) for v in itertools.product(*[S.GRID[k] for k in keys])]
configs.sort(key=lambda c: (c["lb"], c["skip"], c["resid"], c["overlay"] == "funding", c["k"], c["invvol"], c["structure"]))
d, h, lv = S.rows(p, *S.DISC), S.rows(p, *S.HOLD), S.rows(p, *S.LIVE)
rec = []
for c in configs:
    pn = S.build_config_pnl(p, c, S.BPS)
    rec.append(dict(c, name=S.cfg_name(c), disc=E.stats(pn[d])["sharpe"], hold=E.stats(pn[h])["sharpe"],
                    hold_total=E.stats(pn[h])["total"], live_total=E.stats(pn[lv])["total"]))
df = pd.DataFrame(rec)
out = {}
for k in keys:
    g = df.groupby(k).agg(disc_median=("disc", "median"), hold_median=("hold", "median"),
                          hold_pos_share=("hold", lambda x: (x > 0).mean()))
    out[k] = g.round(3).to_dict(orient="index")
    print(f"\n{k}\n{g.round(2).to_string()}")
print("\nall configs: discovery Sharpe>0", round((df.disc > 0).mean(), 2), "holdout Sharpe>0", round((df.hold > 0).mean(), 2),
      "corr(disc, hold)", round(df[["disc", "hold"]].corr().iloc[0, 1], 2))
# the levers one at a time on top of the prod config
prod = dict(lb="M", skip=0, k=8, invvol=False, resid=False, overlay="none", structure="ls")
levers = {"prod (tranched)": {}, "+funding penalty": dict(overlay="funding"), "+inverse vol": dict(invvol=True),
          "+skip 7": dict(skip=7), "+residual": dict(resid=True), "+short lookbacks S": dict(lb="S"),
          "+vol target": dict(overlay="voltarget"), "+funding +invvol": dict(overlay="funding", invvol=True),
          "+funding +invvol +skip7 +resid +S (k8)": dict(overlay="funding", invvol=True, skip=7, resid=True, lb="S")}
lev = {}
for nm, ch in levers.items():
    r = df[np.logical_and.reduce([df[k] == v for k, v in dict(prod, **ch).items()])].iloc[0]
    lev[nm] = dict(disc=r.disc, hold=r.hold, hold_total=r.hold_total, live_total=r.live_total)
    print(f"{nm:<42} discovery SR {r.disc:+.2f} | holdout SR {r.hold:+.2f} total {r.hold_total:+.1f}% | live window {r.live_total:+.1f}%")
top = df.sort_values("disc", ascending=False).head(20)
print("\ntop-20 by discovery on the >=547d universe: holdout Sharpe median", round(top.hold.median(), 2), "positive", int((top.hold > 0).sum()), "/ 20")
json.dump(dict(axes=out, levers=lev, top20_holdout_median=float(top.hold.median()), share_hold_pos=float((df.hold > 0).mean()),
               corr_disc_hold=float(df[["disc", "hold"]].corr().iloc[0, 1])), open(HERE / "families_min547.json", "w"), indent=1, default=float)
