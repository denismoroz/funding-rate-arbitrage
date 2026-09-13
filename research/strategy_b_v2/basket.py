"""Baskets of 4 / 10 / 20 large HL coins, cold-wallet config, strategy vs holding the same basket (run from repo root)."""
import sys, json
sys.path.insert(0, "research/strategy_b_v2"); sys.path.insert(0, "src")
from dataclasses import replace
import numpy as np, pandas as pd
import other_coins as O
import frab.strategy.b2.book as B
from frab.strategy.b2.book import CoinBook, start_book, step
from frab.strategy.b2.params import B2Params

res = json.load(open("research/strategy_b_v2/other_coins.json"))
meta = {r["coin"]: r for r in res if "error" not in r}
# coins shortable on HL for the whole test year, ranked by today's open interest (majors first as they are the largest)
eligible = [r["coin"] for r in sorted(meta.values(), key=lambda r: -(r["oi_musd"] if not r["reference"] else 1e9))
            if r.get("test") and r["coin"] != "PAXG"]
print("eligible by OI:", " ".join(eligible))

def paths(coin, lo, hi):
    r = meta[coin]; lev = O.MAJORS.get(coin, 1.5)
    p = B2Params(coins=(coin,), capital_usd=1000.0, carry_enabled=False, hedge_margin_headroom=1.5, min_order_usd=10.0,
                 short_leverage={coin: lev}, maint_margin_rate={coin: 1 / (2 * r["max_lev"])})
    df, shortable, _ = O.series(coin)
    idx = df.index
    pos = np.flatnonzero((idx >= max(pd.Timestamp(lo, tz="UTC"), shortable.ceil("h"))) & (idx <= pd.Timestamp(hi, tz="UTC")))
    a, b = pos[0], pos[-1]
    px = df["close"].tolist(); hi_ = df["high"].tolist(); fr = df["fundingRate"].tolist()
    book = CoinBook.new(coin, p)
    for i in range(max(0, a - 800), a):
        B.advance_signals(book, px[max(0, i - 730):i + 1], fr[max(0, i - 8):i + 1], p)
    start_book(book, bar_ms=a, price=px[a], params=p)
    eq = []
    for i in range(a, b + 1):
        step(book, bar_ms=i, price=px[i], funding_rate=fr[i], closes=px[max(0, i - 730):i + 1],
             funding_hist=fr[max(0, i - 8):i + 1], params=p, high=hi_[i])
        eq.append(book.equity(px[i]))
    s = pd.Series(eq, index=idx[a:b + 1]); h = pd.Series(1000.0 * np.array(px[a:b + 1]) / px[a], index=idx[a:b + 1])
    return s, h

def dd(x):
    return -(x / x.cummax() - 1).min() * 100

for n in (4, 10, 20):
    coins = eligible[:n]
    print(f"\n== basket of {n}: {' '.join(coins)}")
    for w, (lo, hi) in O.WIN.items():
        got = []
        for c in coins:
            r = meta[c]
            if not r.get(w):
                continue
            got.append(paths(c, lo, hi))
        if len(got) < n * 0.75:
            print(f"  {w:<8} only {len(got)} coins have this window"); continue
        idx = got[0][0].index
        for s, _ in got[1:]: idx = idx.intersection(s.index)
        eq = sum(s.reindex(idx).ffill() for s, _ in got); ho = sum(h.reindex(idx).ffill() for _, h in got)
        cap = 1000.0 * len(got); years = len(idx) / 8760
        ret = eq.iloc[-1] / cap - 1; hret = ho.iloc[-1] / cap - 1
        print(f"  {w:<8} coins {len(got):2d}  strategy {ret*100:+6.1f}% ({((1+ret)**(1/years)-1)*100:+6.1f}%/y) DD {dd(pd.concat([pd.Series([cap]), eq])):4.1f}%"
              f"   | hold {hret*100:+6.1f}% DD {dd(pd.concat([pd.Series([cap]), ho])):4.1f}%")
