"""FRAB x XSMOM on LIVE data: real correlation and what weight the momentum
sleeve deserves. Both series built as daily $ P&L, transfers excluded."""
import sqlite3, numpy as np, pandas as pd, datetime as dt

SCR = "/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad"
fc = sqlite3.connect(f"{SCR}/frab_audit.db")
xc = sqlite3.connect(f"{SCR}/xsmom_audit.db")

# ---- XSMOM daily P&L, rebuilt from positions (ground truth) ----------------
px = {}
for coin, day_ms, close in xc.execute("SELECT coin, day_ms, close FROM xsmom_daily_prices"):
    px.setdefault(coin, {})[day_ms // 86_400_000] = close
def mark(c, d):
    s = px.get(c)
    if not s: return None
    for k in range(d, d-6, -1):
        if k in s: return s[k]
    return None

rows = xc.execute("""SELECT xp.coin, xp.side, p.id, p.qty, p.entry_price, p.status,
                            p.opened_at, p.closed_at
                     FROM xsmom_positions xp JOIN positions p ON p.id=xp.perp_position_id
                     WHERE p.instrument='PERP'""").fetchall()
days = sorted({d for v in px.values() for d in v})
xs = pd.Series(0.0, index=pd.to_datetime([d*86400000 for d in days], unit="ms"))
for coin, side, pid, qty, entry, status, o, cl in rows:
    sgn = 1.0 if side.upper() == "LONG" else -1.0
    d0, d1 = o//86_400_000, (cl or max(days)*86_400_000)//86_400_000
    exitp = None
    if status == "CLOSED":
        cs = "short" if side.upper() == "LONG" else "long"
        f = xc.execute("SELECT price,ts_ms FROM fills WHERE position_id=? AND side=? "
                       "ORDER BY ts_ms DESC LIMIT 1", (pid, cs)).fetchone()
        exitp = f[0] if f and f[1] >= o else mark(coin, d1)
    for d in [x for x in days if d0 <= x <= d1]:
        p_prev = entry if d == d0 else mark(coin, d-1)
        p_now  = (exitp if (status == "CLOSED" and d == d1) else mark(coin, d))
        if p_prev is None or p_now is None: continue
        xs.loc[pd.Timestamp(d*86400000, unit="ms")] += qty*(p_now-p_prev)*sgn
for pid, ts, amt in xc.execute("SELECT position_id, ts_ms, amount FROM funding_accruals"):
    t = pd.Timestamp(ts//86400000*86400000, unit="ms")
    if t in xs.index: xs.loc[t] += amt
for pid, ts, fee in xc.execute("SELECT position_id, ts_ms, fee FROM fills"):
    t = pd.Timestamp(ts//86400000*86400000, unit="ms")
    if t in xs.index: xs.loc[t] -= fee

# ---- FRAB daily P&L from equity deltas (no transfers inside the shared window)
eq = pd.read_sql("SELECT strategy_id sid, ts_ms, total_equity eq FROM equity_snapshots "
                 "ORDER BY ts_ms", fc)
eq["d"] = pd.to_datetime(eq.ts_ms, unit="ms").dt.floor("D")
daily_eq = eq.groupby(["sid","d"]).eq.last().unstack(0)
fr = daily_eq[1].diff()
xs_eq = daily_eq[2].diff()

START = pd.Timestamp("2026-06-17")      # after XSMOM book was fully formed + its 06-16 transfer
END   = pd.Timestamp("2026-09-11")
idx = pd.date_range(START, END)
F = fr.reindex(idx).astype(float)
X = xs.reindex(idx).fillna(0.0).astype(float)
Xe = xs_eq.reindex(idx).astype(float)

print(f"window {START.date()} -> {END.date()}  ({len(idx)} days)\n")
print("validation: XSMOM rebuilt-from-positions vs its own equity deltas")
both = pd.concat([X, Xe], axis=1).dropna()
print(f"   corr = {both.corr().iloc[0,1]:+.3f}   sum: positions ${X.sum():+.2f} vs equity ${Xe.sum():+.2f}")

print(f"\n{'':<8}{'total $':>10}{'mean/day':>11}{'sd/day':>9}{'Sharpe':>9}{'win%':>7}")
print("-"*54)
for nm, s in (("FRAB", F.dropna()), ("XSMOM", X)):
    sh = s.mean()/s.std(ddof=1)*np.sqrt(365)
    print(f"{nm:<8}{s.sum():>10.2f}{s.mean():>11.3f}{s.std(ddof=1):>9.3f}{sh:>9.2f}{(s>0).mean()*100:>7.0f}")

d = pd.concat([F, X], axis=1).dropna(); d.columns = ["FRAB","XSMOM"]
r = d.corr().iloc[0,1]; n = len(d)
se = (1-r**2)/np.sqrt(n-1)
print(f"\nCORRELATION FRAB vs XSMOM = {r:+.3f}   (n={n}, approx 95% CI "
      f"[{r-1.96*se:+.2f}, {r+1.96*se:+.2f}])")
d.to_csv("research/xsmom_live_recon/live_daily_pnl.csv")

# ---- blends -----------------------------------------------------------------
print(f"\n{'weight XSMOM':>13}{'sd/day $':>11}{'mean/day $':>12}{'Sharpe':>9}{'maxDD $':>10}")
print("-"*56)
best=None
for w in (0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0):
    b = d.FRAB + w*d.XSMOM
    sh = b.mean()/b.std(ddof=1)*np.sqrt(365)
    dd = (b.cumsum() - b.cumsum().cummax()).min()
    star = ""
    if best is None or sh > best[1]: best, star = (w, sh), ""
    print(f"{w:>13.2f}{b.std(ddof=1):>11.3f}{b.mean():>12.3f}{sh:>9.2f}{dd:>10.2f}")
print(f"\nbest Sharpe at XSMOM weight = {best[0]:.2f} (Sharpe {best[1]:.2f})")
print("NB: 'weight' scales the CURRENT XSMOM book ($240 notional) against the")
print("    CURRENT FRAB book; 1.0 = today's actual sizing.")

# ---- risk-based sizing (the honest way) --------------------------------------
# Returns cannot be estimated from 87 days; VOLATILITY can. So size by risk,
# never by realized Sharpe on the same sample that chose the weight.
print("\n" + "="*64)
print("RISK DECOMPOSITION (weights from risk only, not from realized return)")
print("="*64)
sF, sX = d.FRAB.std(ddof=1), d.XSMOM.std(ddof=1)
print(f"daily vol:   FRAB ${sF:.3f}   XSMOM ${sX:.3f}   ratio {sX/sF:.1f}x")
var = (d.FRAB + d.XSMOM).var(ddof=1)
cF = (d.FRAB.var(ddof=1) + d.cov().iloc[0,1]) / var
print(f"\nrisk contribution at TODAY'S sizing:  FRAB {cF*100:.1f}%   XSMOM {(1-cF)*100:.1f}%")
w_rp = sF/sX
print(f"\nequal-risk (risk-parity) weight for XSMOM = {w_rp:.3f}")
print(f"  -> XSMOM notional ${240*w_rp:.0f} instead of today's $240")
for lab, w in (("equal risk", w_rp), ("XSMOM 25% of risk", sF/sX/np.sqrt(3)*np.sqrt(1))):
    pass
print("\nskew of each sleeve over the live window (tail direction):")
for nm, s in (("FRAB", d.FRAB), ("XSMOM", d.XSMOM)):
    z = (s-s.mean())/s.std(ddof=1)
    print(f"   {nm:<6} skew {float((z**3).mean()):+6.2f}   worst day ${s.min():+7.2f}   best ${s.max():+7.2f}")
print("\nCAVEAT: FRAB's daily vol UNDERSTATES its real risk — carry pays pennies")
print("  daily and loses in rare jumps (project skew measured -3.57 on the long")
print("  backtest). Risk-parity on daily vol therefore OVER-allocates to FRAB.")
