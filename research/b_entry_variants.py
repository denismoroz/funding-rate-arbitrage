"""Strategy B: four UNTESTED hedge-entry families + their combinations (2026-09-13).

Pre-registered BEFORE running (no tuning after seeing results):
  HYST    enter when 30d return < -5%, exit only when 30d return >= 0% (dead-band)
  TSTAT   30d log-return / (hourly vol * sqrt(720)) < -1.0  (statistically real fall)
  ER      baseline mom14|mom30 AND Kaufman efficiency ratio(30d) > 0.30 (clean trend)
  BREADTH >= 2/3 of the six coins have 30d return < 0 (market-wide downtrend)
Combinations: every pair with AND and with OR, plus vote >=2 of 4 and >=3 of 4.
Already-tested families NOT repeated: momentum sign 3-30d and OR/AND/2of3, close<MA,
drawdown-from-peak, 90/180d regime filters, vol_high, funding/premium gates, btc_down,
Donchian, cross-sectional weakness, continuous hedge ratio, go-to-cash, min-hold.

PASS rule (declared up front): Calmar better than baseline in BOTH halves of history
AND on >= 4 of 6 coins. With 18 variants and a baseline that was itself picked from
~20 in June, anything weaker than that is treated as noise.
"""
import itertools
import numpy as np, pandas as pd
from engine import STAKING_YIELD, load_data, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar, build_trend_up

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
CASH, SLIP, LAG, THR = 0.04, 0.0005, 1, 0.20
data = {c: load_data(c) for c in COINS}
H30 = 30 * 24

def base(close):
    s = pd.Series(close)
    return ((s.pct_change(14*24) < 0) | (s.pct_change(H30) < 0)).fillna(False).values

def hyst(close):
    m = pd.Series(close).pct_change(H30).values
    st, out = False, np.zeros(len(close), dtype=bool)
    for i, v in enumerate(m):
        if not np.isnan(v):
            if not st and v < -0.05: st = True
            elif st and v >= 0.0: st = False
        out[i] = st
    return out

def tstat(close):
    s = pd.Series(close)
    lr = np.log(s).diff()
    t = np.log(s / s.shift(H30)) / (lr.rolling(H30).std() * np.sqrt(H30))
    return (t < -1.0).fillna(False).values

def er_filter(close):
    s = pd.Series(close)
    path = (s - s.shift(24)).abs().rolling(H30).sum() / 24.0
    er = (s - s.shift(H30)).abs() / path
    return base(close) & (er > 0.30).fillna(False).values

# breadth needs all coins on one clock
idx = sorted(set().union(*[set(d.index) for d in data.values()]))
def _down(c):
    m = data[c]["close"].pct_change(H30)
    return (m < 0).astype(float).where(m.notna())
down = pd.DataFrame({c: _down(c) for c in COINS}).reindex(idx)
avail = down.notna().sum(axis=1)
share = down.sum(axis=1) / avail.replace(0, np.nan)
breadth_all = ((share >= 2/3) & (avail >= 3)).fillna(False)

SINGLE = {"HYST": lambda c, close: hyst(close), "TSTAT": lambda c, close: tstat(close),
          "ER": lambda c, close: er_filter(close),
          "BREADTH": lambda c, close: breadth_all.reindex(data[c].index).fillna(False).values}
VARIANTS = {"BASELINE mom14|mom30": lambda c, close: base(close), **SINGLE}
for a, b in itertools.combinations(SINGLE, 2):
    VARIANTS[f"{a} & {b}"] = lambda c, close, a=a, b=b: SINGLE[a](c, close) & SINGLE[b](c, close)
    VARIANTS[f"{a} | {b}"] = lambda c, close, a=a, b=b: SINGLE[a](c, close) | SINGLE[b](c, close)
for k in (2, 3):
    VARIANTS[f"vote >={k} of 4"] = lambda c, close, k=k: np.sum([f(c, close) for f in SINGLE.values()], axis=0) >= k

def seg_stats(pnl):
    eq = TOTAL_CAPITAL + np.cumsum(pnl)
    yrs = len(pnl) / HOURS_PER_YEAR
    cagr = ((eq[-1] / TOTAL_CAPITAL) ** (1/yrs) - 1) * 100 if eq[-1] > 0 else -100.0
    dd = -((eq / np.maximum.accumulate(eq)) - 1).min() * 100
    return cagr, dd, cagr / dd if dd > 0 else np.nan

def main():
    res = {}
    for name, fn in VARIANTS.items():
        per = {}
        for c in COINS:
            df = data[c]; close = df["close"].values; sig = fn(c, close)
            pnl, info = simulate_constdollar(df, STAKING_YIELD.get(c, 0.0), sig, rebal_threshold=THR, risk_free_apr=CASH,
                                             refill_confirm=build_trend_up(close), signal_lag=LAG, slippage=SLIP)
            yrs = len(df) / HOURS_PER_YEAR; h = len(pnl) // 2
            per[c] = dict(full=seg_stats(pnl), h1=seg_stats(pnl[:h]), h2=seg_stats(pnl[h:]),
                          hedge=info["short_realized_pnl"]/TOTAL_CAPITAL/yrs*100,
                          fees=-(info["perp_fees_total"]+info["spot_fees_total"])/TOTAL_CAPITAL/yrs*100,
                          trades=info["trades"]/yrs, on=float(np.mean(sig))*100)
        res[name] = per
        print(f"  done {name}", flush=True)

    B = res["BASELINE mom14|mom30"]
    def agg(per, key, i): return np.nanmean([per[c][key][i] for c in COINS])
    rows = []
    for name, per in res.items():
        beats_coins = sum(per[c]["full"][2] > B[c]["full"][2] for c in COINS)
        h1 = agg(per, "h1", 0) / agg(per, "h1", 1); h2 = agg(per, "h2", 0) / agg(per, "h2", 1)
        bh1 = agg(B, "h1", 0) / agg(B, "h1", 1); bh2 = agg(B, "h2", 0) / agg(B, "h2", 1)
        rows.append(dict(variant=name, CAGR=agg(per, "full", 0), DD=agg(per, "full", 1),
                         Calmar=agg(per, "full", 0)/agg(per, "full", 1), hedge=np.mean([per[c]["hedge"] for c in COINS]),
                         fees=np.mean([per[c]["fees"] for c in COINS]), trades_yr=np.mean([per[c]["trades"] for c in COINS]),
                         hedged=np.mean([per[c]["on"] for c in COINS]), Cal_H1=h1, Cal_H2=h2,
                         coins_better=beats_coins,
                         PASS=(name != "BASELINE mom14|mom30") and h1 > bh1 and h2 > bh2 and beats_coins >= 4))
    T = pd.DataFrame(rows).set_index("variant")
    pd.set_option("display.width", 220)
    print("\n" + T.round(2).sort_values("Calmar", ascending=False).to_string())
    T.round(3).to_csv("b_entry_variants_results.csv")


if __name__ == "__main__":
    main()
