"""Long-history XSMOM sim on fresh Binance daily candles, reusing the LIVE
engine compute_scores() and live params (n=16, lb=[14,21,30,45,60], weekly Thu)."""
import sys, glob, os, datetime as dt
sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/src")
import numpy as np, pandas as pd
from frab.strategy.xsmom.evaluators.signal import compute_scores

D = "/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/619a0272-724d-477f-be5a-cd442e6762db/scratchpad/daily"
DAY_MS=86_400_000; LB=(14,21,30,45,60); K=8
COST_BPS_LEG=4.4   # per leg one-way (live-measured HL perp cost)

series={}
for f in glob.glob(D+"/*.csv"):
    coin=os.path.basename(f)[:-4]
    df=pd.read_csv(f)
    # normalize closeTime -> UTC midnight grid day
    df["day"]=(df["day_ms"]//DAY_MS)*DAY_MS
    series[coin]=df.groupby("day")["close"].last()
panel=pd.DataFrame(series).sort_index()
grid=panel.index.values.astype(np.int64)
def wd(ms): return dt.datetime.utcfromtimestamp(ms/1000).weekday()
def dstr(ms): return dt.datetime.utcfromtimestamp(ms/1000).strftime("%Y-%m-%d")

def book_asof(day_ms):
    sub={}
    for coin in panel.columns:
        s=panel[coin]; s=s[(s.index<=day_ms)].dropna()
        if len(s)==0: continue
        sub[coin]=list(zip(s.index.astype(np.int64).tolist(), s.values.tolist()))
    sc=compute_scores(sub, LB)
    ranked=sorted(sc.items(), key=lambda t:t[1], reverse=True)
    if len(ranked)<2*K: return None,None
    return [x[0] for x in ranked[:K]], [x[0] for x in ranked[-K:]]

def ret(coin,d0,d1):
    p0=panel[coin].get(d0); p1=panel[coin].get(d1)
    if p0 is None or p1 is None or np.isnan(p0) or np.isnan(p1): return None
    return p1/p0-1.0

reb=[d for d in grid if wd(d)==3]
prev_book=set()
rows_gross=[]; rows_net=[]; recs=[]
for i in range(len(reb)-1):
    d0,d1=reb[i],reb[i+1]
    L,S=book_asof(d0)
    if not L: continue
    lr=[ret(x,d0,d1) for x in L]; sr=[ret(x,d0,d1) for x in S]
    lr=[x for x in lr if x is not None]; sr=[x for x in sr if x is not None]
    if not lr or not sr: continue
    gross=0.5*np.mean(lr)-0.5*np.mean(sr)
    # turnover cost: legs changed vs prev book, each changed leg = enter+exit later
    cur=set(("L",x) for x in L)|set(("S",x) for x in S)
    changed=len(cur.symmetric_difference(prev_book))/2  # positions opened this rebal
    # cost as fraction of book: each leg notional = book/(2K); one-way per changed open + eventual close ~2 legs
    cost_frac = (changed*2)*(COST_BPS_LEG/1e4)/(2*K)
    net=gross-cost_frac
    prev_book=cur
    rows_gross.append(gross); rows_net.append(net); recs.append((dstr(d0),gross,net))

g=np.array(rows_gross); n=np.array(rows_net)
def stats(a,label):
    cum=np.prod(1+a)-1
    sharpe=(a.mean()/a.std(ddof=0))*np.sqrt(52) if a.std()>0 else float('nan')
    eq=np.cumprod(1+a); peak=np.maximum.accumulate(eq); dd=(eq/peak-1).min()
    print(f"{label:6}: periods={len(a)}  mean/wk={a.mean()*100:+.3f}%  Sharpe(ann)={sharpe:5.2f}  "
          f"cum={cum*100:+.1f}%  maxDD={dd*100:.1f}%  win={ (a>0).mean()*100:.0f}%")
print(f"span: {recs[0][0]} -> {recs[-1][0]}   weekly Thu rebalances, n_pos=16, lb={LB}")
stats(g,"GROSS"); stats(n,"NET")
# half-year splits
half=len(g)//2
stats(g[:half],"G_H1"); stats(g[half:],"G_H2")
# recent (matches live era)
if len(g)>=8: stats(g[-8:],"G_last8")
print("\nlast 6 weekly gross/net:")
for a,gg,nn in recs[-6:]: print(f"  {a}: gross={gg*100:+.2f}%  net={nn*100:+.2f}%")
