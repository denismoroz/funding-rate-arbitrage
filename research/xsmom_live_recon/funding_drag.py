"""Is XSMOM's funding bill a systematic structural cost the backtest omits?
Momentum longs winners (crowded longs pay funding) and shorts losers (crowded
shorts... or not). Measure sign, size, and consistency per leg."""
import sqlite3, numpy as np, datetime as dt
DB="/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/xsmom_audit.db"
c=sqlite3.connect(DB)

rows=c.execute("""
 SELECT xp.side, xp.coin, p.id, p.qty, p.entry_price, p.opened_at,
        COALESCE(p.closed_at, (SELECT MAX(day_ms) FROM xsmom_daily_prices)) ca,
        (SELECT COALESCE(SUM(amount),0) FROM funding_accruals fa WHERE fa.position_id=p.id)
 FROM xsmom_positions xp JOIN positions p ON p.id=xp.perp_position_id
 WHERE p.instrument='PERP'""").fetchall()

agg={"LONG":[0.0,0.0,0,0], "SHORT":[0.0,0.0,0,0]}   # funding, notional*years, n, n_neg
for side,coin,pid,qty,entry,o,ca,f in rows:
    notional=qty*entry
    years=max((ca-o)/1000/86400/365.25, 1e-9)
    a=agg[side]; a[0]+=f; a[1]+=notional*years; a[2]+=1; a[3]+= (f<0)

print(f"{'leg':<6} {'n':>4} {'funding$':>10} {'notional·yr':>12} {'ann rate':>9} {'% legs paying':>14}")
print("-"*62)
tot_f=tot_ny=0
for side in ("LONG","SHORT"):
    f,ny,n,neg=agg[side]; tot_f+=f; tot_ny+=ny
    print(f"{side:<6} {n:>4} {f:>10.2f} {ny:>12.3f} {f/ny*100:>8.2f}% {neg/n*100:>13.0f}%")
print("-"*62)
print(f"{'BOTH':<6} {sum(a[2] for a in agg.values()):>4} {tot_f:>10.2f} {tot_ny:>12.3f} {tot_f/tot_ny*100:>8.2f}%")
print(f"\n=> book-level funding drag = {tot_f/tot_ny*100:+.2f}% per year of gross notional")

# weekly consistency
wk=c.execute("""SELECT strftime('%Y-%W', fa.ts_ms/1000,'unixepoch') w, SUM(fa.amount)
 FROM funding_accruals fa JOIN positions p ON p.id=fa.position_id
 JOIN xsmom_positions xp ON xp.perp_position_id=p.id GROUP BY w ORDER BY w""").fetchall()
v=np.array([x[1] for x in wk])
print(f"weekly funding: n={len(v)}  negative in {int((v<0).sum())}/{len(v)} weeks  "
      f"mean=${v.mean():+.3f}  total=${v.sum():+.2f}")
print("   " + "  ".join(f"{x:+.2f}" for x in v))

# worst payers
print("\ntop funding payers (coin, leg):")
q=c.execute("""SELECT xp.coin, xp.side, SUM(fa.amount) s FROM funding_accruals fa
 JOIN positions p ON p.id=fa.position_id JOIN xsmom_positions xp ON xp.perp_position_id=p.id
 GROUP BY xp.coin, xp.side ORDER BY s LIMIT 8""").fetchall()
for coin,side,s in q: print(f"   {coin:<7} {side:<6} {s:+.3f}")
