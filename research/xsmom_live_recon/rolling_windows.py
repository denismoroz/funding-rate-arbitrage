"""Where does the live 13-week run sit in the backtest's own distribution of
13-week windows? Plus the Sharpe-vs-window-choice ladder."""
import sys, glob, os, datetime as dt
sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/src")
import numpy as np, pandas as pd
from frab.strategy.xsmom.evaluators.signal import compute_scores

D = "/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/daily"
DAY_MS=86_400_000; LB=(14,21,30,45,60); K=8; COST=4.4

series={}
for f in glob.glob(D+"/*.csv"):
    coin=os.path.basename(f)[:-4]; df=pd.read_csv(f)
    df["day"]=(df["day_ms"]//DAY_MS)*DAY_MS
    series[coin]=df.groupby("day")["close"].last()
panel=pd.DataFrame(series).sort_index()
grid=panel.index.values.astype(np.int64)
wd=lambda ms: dt.datetime.fromtimestamp(ms/1000, dt.UTC).weekday()
ds=lambda ms: dt.datetime.fromtimestamp(ms/1000, dt.UTC).strftime("%Y-%m-%d")

def book(day_ms):
    sub={}
    for coin in panel.columns:
        s=panel[coin]; s=s[s.index<=day_ms].dropna()
        if len(s)>max(LB): sub[coin]=list(zip(s.index.astype(np.int64).tolist(), s.values.tolist()))
    sc=compute_scores(sub, LB)
    r=sorted(sc.items(), key=lambda t:t[1], reverse=True)
    return ([x[0] for x in r[:K]], [x[0] for x in r[-K:]]) if len(r)>=2*K else (None,None)

def ret(c,d0,d1):
    p0,p1=panel[c].get(d0),panel[c].get(d1)
    return None if (p0 is None or p1 is None or np.isnan(p0) or np.isnan(p1)) else p1/p0-1

reb=[d for d in grid if wd(d)==3]
prev=set(); recs=[]
for i in range(len(reb)-1):
    d0,d1=reb[i],reb[i+1]
    L,S=book(d0)
    if not L: continue
    lr=[x for x in (ret(y,d0,d1) for y in L) if x is not None]
    sr=[x for x in (ret(y,d0,d1) for y in S) if x is not None]
    if not lr or not sr: continue
    g=0.5*np.mean(lr)-0.5*np.mean(sr)
    cur=set(("L",x) for x in L)|set(("S",x) for x in S)
    ch=len(cur.symmetric_difference(prev))/2 if prev else 2*K
    prev=cur
    recs.append((ds(d0), g, g-(ch*2)*(COST/1e4)/(2*K)))

net=np.array([r[2] for r in recs]); dates=[r[0] for r in recs]
W=13
roll=np.array([np.prod(1+net[i:i+W])-1 for i in range(len(net)-W+1)])
LIVE=-0.1119
pct=(roll<LIVE).mean()*100
print(f"history: {dates[0]} -> {dates[-1]},  {len(net)} weeks,  {len(roll)} overlapping {W}-week windows")
print(f"\n{W}-week window distribution (NET):")
for q in (1,5,10,25,50,75,90,99):
    print(f"   p{q:<3}= {np.percentile(roll,q)*100:+7.2f}%")
print(f"   mean= {roll.mean()*100:+7.2f}%   worst= {roll.min()*100:+7.2f}%   best= {roll.max()*100:+7.2f}%")
print(f"\nLIVE 13wk = {LIVE*100:+.2f}%  ->  percentile {pct:.1f}%  "
      f"({(roll<LIVE).sum()} of {len(roll)} historical windows were worse)")
print(f"P(window <= live) = {(roll<=LIVE).mean()*100:.1f}%")

# worst windows in history
order=np.argsort(roll)[:6]
print(f"\nworst {W}-week windows in history:")
for i in order: print(f"   {dates[i]} -> {dates[i+W-1]}: {roll[i]*100:+.2f}%")

# Sharpe ladder by start year
print("\nSharpe(ann) depending on window START (same data, same params):")
for y in (2021,2022,2023,2024):
    m=[i for i,d in enumerate(dates) if int(d[:4])>=y]
    if len(m)<60: continue
    a=net[m[0]:]
    print(f"   {y}+ : weeks={len(a):3d}  mean/wk={a.mean()*100:+.3f}%  "
          f"Sharpe={a.mean()/a.std(ddof=1)*np.sqrt(52):.2f}  cum={(np.prod(1+a)-1)*100:+.0f}%")

# trailing-12-month rolling Sharpe: is the edge decaying?
print("\nrolling 52-week Sharpe (is the edge fading?):")
for i in range(0, len(net)-52, 26):
    a=net[i:i+52]
    print(f"   {dates[i]} : Sharpe={a.mean()/a.std(ddof=1)*np.sqrt(52):+5.2f}  cum={(np.prod(1+a)-1)*100:+7.1f}%")
a=net[-52:]
print(f"   {dates[-52]} : Sharpe={a.mean()/a.std(ddof=1)*np.sqrt(52):+5.2f}  cum={(np.prod(1+a)-1)*100:+7.1f}%  <-- last 12m")
