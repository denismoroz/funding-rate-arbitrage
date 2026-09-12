"""What the MODEL would have earned over the exact live weeks, on the exact
prod price panel and universe the live engine saw. Isolates market-vs-execution."""
import sqlite3, datetime as dt, os, sys, json
sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/src")
import numpy as np
from frab.strategy.xsmom.evaluators.signal import compute_scores

DB = "/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/xsmom_audit.db"
c = sqlite3.connect(DB)
LB = (14, 21, 30, 45, 60); K = 8; COST_BPS_LEG = 4.4
UNIVERSE = set(json.loads(c.execute("SELECT params_json FROM strategies WHERE id=2").fetchone()[0])["universe"])

def dstr(ms): return dt.datetime.utcfromtimestamp(ms/1000).strftime("%Y-%m-%d")

series = {}
for coin, day_ms, close in c.execute("SELECT coin, day_ms, close FROM xsmom_daily_prices ORDER BY day_ms"):
    series.setdefault(coin, []).append((day_ms, close))

def book_asof(ts):
    sub = {k: [(d, p) for d, p in v if d <= ts] for k, v in series.items() if k in UNIVERSE}
    sub = {k: v for k, v in sub.items() if len(v) >= max(LB) + 1}
    sc = compute_scores(sub, LB)
    ranked = sorted(sc.items(), key=lambda t: t[1], reverse=True)
    return [x[0] for x in ranked[:K]], [x[0] for x in ranked[-K:]]

def price_at(coin, ts):
    v = series.get(coin)
    if not v: return None
    cand = [p for d, p in v if d <= ts]
    return cand[-1] if cand else None

edges = [r[0] for r in c.execute("SELECT DISTINCT opened_at FROM xsmom_positions ORDER BY opened_at")]
bounds = []
for t in edges:
    if not bounds or dstr(t) != dstr(bounds[-1]): bounds.append(t)
NOW = c.execute("SELECT MAX(day_ms) FROM xsmom_daily_prices").fetchone()[0]
E = bounds + [NOW]

live = {w["t0"]: w for w in json.load(open("research/xsmom_live_recon/live_weekly.json"))}

print(f"{'week':<12} {'model%':>8} {'modelNet%':>10} {'live%':>8} {'diff%':>8}   ovl L    S")
print("-" * 74)
rows = []
prev = set()
for i in range(len(E)-1):
    t0, t1 = E[i], E[i+1]
    L, S = book_asof(t0)
    lr, sr = [], []
    for x in L:
        a, b = price_at(x, t0), price_at(x, t1)
        if a and b: lr.append(b/a - 1)
    for x in S:
        a, b = price_at(x, t0), price_at(x, t1)
        if a and b: sr.append(b/a - 1)
    gross = 0.5*np.mean(lr) - 0.5*np.mean(sr)
    cur = set(("L", x) for x in L) | set(("S", x) for x in S)
    changed = len(cur.symmetric_difference(prev))/2 if prev else 2*K
    cost = (changed*2)*(COST_BPS_LEG/1e4)/(2*K)
    prev = cur
    net = gross - cost
    lv = live.get(t0, {}).get("ret", float('nan'))
    # overlap = FULL live book just after this rebalance (KEEPs + new), vs model book
    ref = t0 + 1800_000
    live_legs = set(c.execute(
        "SELECT xp.side, xp.coin FROM xsmom_positions xp JOIN positions p ON p.id=xp.perp_position_id "
        "WHERE p.opened_at<=? AND (p.closed_at IS NULL OR p.closed_at>?)", (ref, ref)).fetchall())
    live_L = {x[1] for x in live_legs if x[0] == "LONG"}
    live_S = {x[1] for x in live_legs if x[0] == "SHORT"}
    ov = f"{len(live_L & set(L))}/{len(live_L)} {len(live_S & set(S))}/{len(live_S)}"
    rows.append(dict(t0=t0, gross=gross, net=net, live=lv))
    print(f"{dstr(t0):<12} {gross*100:>8.2f} {net*100:>10.2f} {lv*100:>8.2f} "
          f"{(lv-net)*100:>8.2f}   {ov}")

g = np.array([r["gross"] for r in rows]); n = np.array([r["net"] for r in rows])
l = np.array([r["live"] for r in rows])
print("-" * 74)
def st(a, lab):
    sh = a.mean()/a.std(ddof=1)*np.sqrt(52) if a.std() > 0 else float('nan')
    print(f"{lab:<10} mean/wk={a.mean()*100:+.3f}%  sd={a.std(ddof=1)*100:.2f}%  "
          f"Sharpe={sh:+.2f}  cum={(np.prod(1+a)-1)*100:+.2f}%  win={(a>0).mean()*100:.0f}%")
st(g, "MODEL gr"); st(n, "MODEL net"); st(l, "LIVE")
d = l - n
print(f"\ndrag live-vs-model: mean={d.mean()*100:+.3f}%/wk  sd={d.std(ddof=1)*100:.2f}%  "
      f"total={(d.sum())*100:+.2f}%   corr(live,model)={np.corrcoef(l, n)[0,1]:+.3f}")
json.dump([{k: round(float(v), 6) if isinstance(v, float) else v for k, v in r.items()} for r in rows],
          open("research/xsmom_live_recon/model_weekly.json", "w"), indent=1)
