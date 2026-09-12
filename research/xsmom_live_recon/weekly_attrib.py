"""XSMOM live weekly attribution: real P&L per rebalance-week, marked to daily closes.

Independent of equity_snapshots (which are zero for xsmom by design — see memory).
Ground truth = positions + fills + funding_accruals + xsmom_daily_prices.
"""
import sqlite3, datetime as dt, os, sys
import numpy as np

DB = os.environ.get("XSMOM_DB", "/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/xsmom_audit.db")
c = sqlite3.connect(DB)

def dstr(ms): return dt.datetime.utcfromtimestamp(ms/1000).strftime("%Y-%m-%d")

# ---- daily close panel -------------------------------------------------
px = {}
for coin, day_ms, close in c.execute("SELECT coin, day_ms, close FROM xsmom_daily_prices"):
    px.setdefault(coin, {})[day_ms // 86_400_000] = close   # key = day index
days_all = sorted({d for v in px.values() for d in v})

def mark(coin, day_idx):
    """close of `coin` on day_idx, walking back up to 5 days."""
    s = px.get(coin)
    if not s: return None
    for k in range(day_idx, day_idx - 6, -1):
        if k in s: return s[k]
    return None

# ---- rebalance boundaries (real live timestamps) ------------------------
reb = [r[0] for r in c.execute(
    "SELECT DISTINCT opened_at FROM xsmom_positions ORDER BY opened_at")]
# collapse timestamps within the same day into one boundary
bounds = []
for t in reb:
    if not bounds or dstr(t) != dstr(bounds[-1]): bounds.append(t)
NOW = c.execute("SELECT MAX(day_ms) FROM xsmom_daily_prices").fetchone()[0]
edges = bounds + [NOW]

# ---- positions ---------------------------------------------------------
rows = c.execute("""
  SELECT xp.id, xp.coin, xp.side, xp.state, p.id, p.qty, p.entry_price,
         p.status, p.opened_at, p.closed_at
  FROM xsmom_positions xp JOIN positions p ON p.id = xp.perp_position_id
  WHERE p.instrument='PERP'
  ORDER BY p.opened_at
""").fetchall()

def sgn(side): return 1.0 if side.upper() == "LONG" else -1.0

positions = []
for xid, coin, side, state, pid, qty, entry, status, o, cl in rows:
    # exit price: closing fill if present, else daily close on close date
    exitp, exit_src = None, None
    if status == "CLOSED":
        cs = "short" if side.upper() == "LONG" else "long"
        f = c.execute("SELECT price, ts_ms FROM fills WHERE position_id=? AND side=? "
                      "ORDER BY ts_ms DESC LIMIT 1", (pid, cs)).fetchone()
        if f and f[1] >= o:      # genuine closing fill (after open)
            exitp, exit_src = f[0], "fill"
        else:
            exitp, exit_src = mark(coin, (cl or NOW)//86_400_000), "mark"
    positions.append(dict(xid=xid, coin=coin, side=side, pid=pid, qty=qty, entry=entry,
                          status=status, o=o, cl=cl, exitp=exitp, exit_src=exit_src))

# ---- weekly attribution ------------------------------------------------
print(f"{'week':<12} {'->':<12} {'price':>9} {'fund':>8} {'fees':>8} {'net$':>9} "
      f"{'book$':>8} {'ret%':>7} {'nPos':>5}")
print("-"*92)
weeks = []
for i in range(len(edges)-1):
    t0, t1 = edges[i], edges[i+1]
    d0, d1 = t0//86_400_000, t1//86_400_000
    wp = wf = wfee = 0.0
    book = 0.0; npos = 0
    for P in positions:
        if P["o"] >= t1: continue                    # not yet opened
        if P["cl"] is not None and P["cl"] <= t0: continue  # already closed
        # entry ref: real entry if opened this week, else close of prev boundary day
        if P["o"] >= t0:
            p_start = P["entry"]
        else:
            p_start = mark(P["coin"], d0)
        # exit ref: real exit if closed this week, else close of this boundary day
        if P["cl"] is not None and P["cl"] <= t1:
            p_end = P["exitp"]
        else:
            p_end = mark(P["coin"], d1)
        if p_start is None or p_end is None: continue
        wp += P["qty"] * (p_end - p_start) * sgn(P["side"])
        # time-weighted notional: a leg closed 14s into the week must not
        # inflate the denominator with its full notional
        live0 = max(P["o"], t0); live1 = min(P["cl"] or t1, t1)
        w = max(0.0, (live1 - live0) / (t1 - t0))
        book += P["qty"] * p_start * w
        if w > 0.05: npos += 1
        wf += c.execute("SELECT COALESCE(SUM(amount),0) FROM funding_accruals "
                        "WHERE position_id=? AND ts_ms>? AND ts_ms<=?",
                        (P["pid"], t0, t1)).fetchone()[0]
        wfee += c.execute("SELECT COALESCE(SUM(fee),0) FROM fills "
                          "WHERE position_id=? AND ts_ms>=? AND ts_ms<=?",
                          (P["pid"], t0, t1)).fetchone()[0]
    net = wp + wf - wfee
    ret = net/book*100 if book > 0 else float('nan')
    weeks.append(dict(t0=t0, t1=t1, price=wp, fund=wf, fees=wfee, net=net, book=book, ret=ret/100, npos=npos))
    print(f"{dstr(t0):<12} {dstr(t1):<12} {wp:>9.2f} {wf:>8.2f} {wfee:>8.3f} {net:>9.2f} "
          f"{book:>8.0f} {ret:>7.2f} {npos:>5}")

print("-"*92)
tp = sum(w['price'] for w in weeks); tf = sum(w['fund'] for w in weeks); tfe = sum(w['fees'] for w in weeks)
print(f"{'TOTAL':<25} {tp:>9.2f} {tf:>8.2f} {tfe:>8.3f} {tp+tf-tfe:>9.2f}")
r = np.array([w['ret'] for w in weeks])
cum = np.prod(1+r)-1
sh = r.mean()/r.std(ddof=1)*np.sqrt(52) if r.std() > 0 else float('nan')
print(f"\nweeks={len(r)}  mean/wk={r.mean()*100:+.3f}%  sd/wk={r.std(ddof=1)*100:.2f}%  "
      f"Sharpe(ann)={sh:+.2f}  cum={cum*100:+.2f}%  win={(r>0).mean()*100:.0f}%")
eq = np.cumprod(1+r); dd = (eq/np.maximum.accumulate(eq)-1).min()
print(f"maxDD={dd*100:.2f}%   avg book=${np.mean([w['book'] for w in weeks]):.0f}")

import json
json.dump([{k: (v if not isinstance(v, float) else round(v, 6)) for k, v in w.items()} for w in weeks],
          open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_weekly.json"), "w"), indent=1)
print("\nwrote live_weekly.json")
