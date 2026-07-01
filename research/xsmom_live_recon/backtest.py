"""Period-matched + long-history XSMOM backtest reusing the LIVE engine's
compute_scores(). Reconciles against live selection and PnL."""
import sys, sqlite3, datetime as dt
sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/src")
import numpy as np, pandas as pd
from frab.strategy.xsmom.evaluators.signal import compute_scores

DB = "/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/619a0272-724d-477f-be5a-cd442e6762db/scratchpad/frab_prod.db"
DAY_MS = 86_400_000
c = sqlite3.connect(DB)

UNIVERSE = ["AAVE","ADA","APT","ARB","ATOM","AVAX","BCH","BNB","BTC","CRV","DOGE","DOT",
            "EIGEN","ENA","ETH","INJ","JTO","JUP","LINK","LTC","NEAR","PENDLE","PYTH","SOL",
            "SUI","TAO","TRX","UNI","WLD","XLM","XRP","ZRO"]
LOOKBACKS = (14,21,30,45,60)
N_POS = 16
K = N_POS//2  # 8 per side

# ---- load panel: DataFrame[day x coin] of closes ----
raw = c.execute("SELECT coin, day_ms, close FROM xsmom_daily_prices ORDER BY day_ms").fetchall()
by_coin = {}
for coin, day, close in raw:
    by_coin.setdefault(coin, []).append((day, close))
days = sorted({d for coin in by_coin for d,_ in by_coin[coin]})
grid = np.arange(min(days), max(days)+DAY_MS, DAY_MS, dtype=np.int64)
panel = pd.DataFrame({coin: pd.Series({d:p for d,p in rows}) for coin,rows in by_coin.items()}).reindex(grid)
panel = panel[[c_ for c_ in UNIVERSE if c_ in panel.columns]]
def dstr(ms): return dt.datetime.utcfromtimestamp(ms/1000).strftime("%Y-%m-%d")

def scores_asof(day_ms):
    """Run the LIVE engine compute_scores using history up to and including day_ms."""
    sub = {}
    for coin in panel.columns:
        s = panel[coin]
        s = s[s.index <= day_ms].dropna()
        sub[coin] = list(zip(s.index.astype(np.int64).tolist(), s.values.tolist()))
    return compute_scores(sub, LOOKBACKS)

def book_asof(day_ms):
    sc = scores_asof(day_ms)
    ranked = sorted(sc.items(), key=lambda t:t[1], reverse=True)
    longs = [x[0] for x in ranked[:K]]
    shorts= [x[0] for x in ranked[-K:]]
    return longs, shorts

# ===== 1) SIGNAL PARITY at live rebalances =====
live_books = {
 "2026-06-15": (["WLD","JTO","NEAR","XLM","CRV","INJ","TAO","ATOM"],
                ["BCH","APT","AVAX","ADA","DOT","UNI","ETH","ARB"]),
}
print("="*70)
print("SIGNAL PARITY — engine compute_scores vs live-opened book")
print("="*70)
for anchor in ["2026-06-14","2026-06-15","2026-06-17","2026-06-18","2026-06-24","2026-06-25"]:
    a = int(dt.datetime.strptime(anchor,"%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()*1000)
    # align to grid day (23:59:59 close). find closest grid day <= end of that date
    end = a + DAY_MS - 1
    gday = max([d for d in grid if d <= end])
    L,S = book_asof(gday)
    print(f"\nas-of {anchor} (grid {dstr(gday)}):")
    print(f"  LONG : {sorted(L)}")
    print(f"  SHORT: {sorted(S)}")

# ===== 2) LIVE-WINDOW clean price PnL (backtest) for the 06-15 initial book =====
print("\n"+"="*70)
print("LIVE-WINDOW backtest: clean price PnL of the 06-15 book, held to now")
print("="*70)
L,S = live_books["2026-06-15"]
last_day = grid[-1]
entry_day = max([d for d in grid if d <= int(dt.datetime(2026,6,15,tzinfo=dt.timezone.utc).timestamp()*1000)+DAY_MS-1])
per_leg = 15.0  # approx live per-leg notional
def leg_ret(coin, d0, d1):
    p0 = panel[coin].get(d0); p1 = panel[coin].get(d1)
    if p0 is None or p1 is None or np.isnan(p0) or np.isnan(p1): return None
    return p1/p0 - 1.0
tot=0.0
for coin in L:
    r=leg_ret(coin,entry_day,last_day); pnl=per_leg*r if r is not None else 0
    tot+=pnl
for coin in S:
    r=leg_ret(coin,entry_day,last_day); pnl=-per_leg*r if r is not None else 0
    tot+=pnl
print(f"entry grid day={dstr(entry_day)}  exit={dstr(last_day)}  per_leg=${per_leg}")
print(f"clean price PnL of initial book held to now (no rebal, no cost) = ${tot:.2f}")

# ===== 3) LONG-HISTORY weekly-rebalance sim (return space) =====
print("\n"+"="*70)
print("LONG-HISTORY weekly rebalance sim (Thursdays), gross, dollar-neutral")
print("="*70)
# weekly rebalance on Thursdays present in grid
reb_days = [d for d in grid if dt.datetime.utcfromtimestamp(d/1000).weekday()==3]
rets=[]  # per-period book return (fraction of book)
recs=[]
for i in range(len(reb_days)-1):
    d0, d1 = reb_days[i], reb_days[i+1]
    L,S = book_asof(d0)
    if len(L)<K or len(S)<K: continue
    lr=[leg_ret(x,d0,d1) for x in L]; sr=[leg_ret(x,d0,d1) for x in S]
    lr=[x for x in lr if x is not None]; sr=[x for x in sr if x is not None]
    if not lr or not sr: continue
    # book return = 0.5*mean(long) - 0.5*mean(short)   (per_side=book/2 each)
    br = 0.5*np.mean(lr) - 0.5*np.mean(sr)
    rets.append(br); recs.append((dstr(d0),dstr(d1),br))
rets=np.array(rets)
if len(rets):
    cum=np.prod(1+rets)-1
    ann_periods=52
    sharpe=(rets.mean()/rets.std(ddof=0))*np.sqrt(ann_periods) if rets.std()>0 else float('nan')
    print(f"periods={len(rets)}  first={recs[0][0]}  last={recs[-1][1]}")
    print(f"mean/period={rets.mean()*100:.3f}%  std={rets.std()*100:.3f}%  gross Sharpe(ann)={sharpe:.2f}")
    print(f"cumulative gross book return={cum*100:.2f}%")
    print(f"win rate={(rets>0).mean()*100:.1f}%")
    # cost drag: ~2 legs turnover * cost. Apply 4.4bps/leg roundtrip approx per rebal on churn
    print("\nper-period returns:")
    for a,b,r in recs: print(f"  {a}->{b}: {r*100:+.3f}%")
