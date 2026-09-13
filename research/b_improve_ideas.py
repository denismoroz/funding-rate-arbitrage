"""Strategy B: four improvement ideas x three entry points (2026-09-13).

Pre-registered before running:
  ideas   NONE       current strategy logic
          STICKY24   enter immediately, exit only after the entry signal has been OFF 24h in a row
          PARTIAL50  short 50% of spot units instead of 100%
          BTC_HEDGE  hedge with the BTC perp, sized by the coin's rolling 90d beta to BTC (clip 0.5..2)
          SPOT75     75% of capital in spot, 25% cash (reserve generalised)
          MAKER      reference only: June's maker-ish execution, slippage 2 bps instead of 5
  entries BASE (mom14|mom30), HYST (-5%/0% on 30d), MOM30 (30d return < 0)
  PASS vs the current strategy (BASE+NONE): Calmar better in BOTH halves, better on >= 4/6
       coins, AND second-half mean max drawdown not deeper by more than 2 pp (B exists to
       protect in bear markets; HYST taught that cheaper-but-leakier is not an improvement).
"""
import numpy as np, pandas as pd
from engine import STAKING_YIELD, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import build_trend_up
from b_sim_ext import simulate_ext
import b_entry_variants as V

COINS, data = V.COINS, V.data
btc = data["BTC"]

def mom30(close):
    return (pd.Series(close).pct_change(720) < 0).fillna(False).values

ENTRIES = {"BASE": V.base, "HYST": V.hyst, "MOM30": mom30}

def sticky(sig, hours=24):
    out = np.zeros(len(sig), dtype=bool); on = False; off = 0
    for i, s in enumerate(sig):
        if s: on, off = True, 0
        elif on:
            off += 1
            if off >= hours: on = False
        out[i] = on
    return out

def btc_inputs(c):
    df = data[c]
    bc = btc["close"].reindex(df.index).ffill().bfill().values
    br = btc["fundingRate"].reindex(df.index).fillna(0.0).values
    rc = np.log(df["close"]).diff(); rb = np.log(pd.Series(bc, index=df.index)).diff()
    beta = (rc.rolling(2160).cov(rb) / rb.rolling(2160).var()).shift(1).clip(0.5, 2.0).fillna(1.0).values
    return bc, br, beta

def run_cfg(c, entry, idea):
    df = data[c]; close = df["close"].values; sig = ENTRIES[entry](close)
    kw = dict(rebal_threshold=V.THR, risk_free_apr=V.CASH, refill_confirm=build_trend_up(close),
              signal_lag=V.LAG, slippage=V.SLIP)
    if idea == "STICKY24": sig = sticky(sig, 24)
    elif idea == "PARTIAL50": kw["hedge_ratio"] = 0.5
    elif idea == "BTC_HEDGE":
        bc, br, beta = btc_inputs(c); kw.update(hedge_close=bc, hedge_rates=br, beta=beta)
    elif idea == "SPOT75": kw["position_size"] = 1500.0
    elif idea == "MAKER": kw["slippage"] = 0.0002
    pnl, info = simulate_ext(df, STAKING_YIELD.get(c, 0.0), sig, **kw)
    yrs = len(df) / HOURS_PER_YEAR; h = len(pnl) // 2
    seg = V.seg_stats if hasattr(V, "seg_stats") else None
    def st(p):
        eq = TOTAL_CAPITAL + np.cumsum(p); y = len(p) / HOURS_PER_YEAR
        cg = ((eq[-1]/TOTAL_CAPITAL)**(1/y)-1)*100 if eq[-1] > 0 else -100.0
        dd = -((eq/np.maximum.accumulate(eq))-1).min()*100
        return cg, dd
    return dict(full=st(pnl), h1=st(pnl[:h]), h2=st(pnl[h:]),
                hedge=info["short_realized_pnl"]/TOTAL_CAPITAL/yrs*100,
                fees=-(info["perp_fees_total"]+info["spot_fees_total"])/TOTAL_CAPITAL/yrs*100,
                trades=info["trades"]/yrs)

IDEAS = ["NONE", "STICKY24", "PARTIAL50", "BTC_HEDGE", "SPOT75", "MAKER"]

def main():
    res = {}
    for e in ENTRIES:
        for idea in IDEAS:
            res[(e, idea)] = {c: run_cfg(c, e, idea) for c in COINS}
            print(f"  done {e}+{idea}", flush=True)
    ref = res[("BASE", "NONE")]
    m = lambda per, k, j: np.mean([per[c][k][j] for c in COINS])
    cal = lambda per, k: m(per, k, 0) / m(per, k, 1)
    rows = []
    for (e, idea), per in res.items():
        own = res[(e, "NONE")]
        better = sum(per[c]["full"][0]/per[c]["full"][1] > ref[c]["full"][0]/ref[c]["full"][1] for c in COINS)
        guard = m(per, "h2", 1) <= m(ref, "h2", 1) + 2.0
        rows.append(dict(entry=e, idea=idea, CAGR=m(per, "full", 0), DD=m(per, "full", 1), Calmar=cal(per, "full"),
                         Cal_H1=cal(per, "h1"), Cal_H2=cal(per, "h2"), DD_H2=m(per, "h2", 1),
                         hedge=np.mean([per[c]["hedge"] for c in COINS]), fees=np.mean([per[c]["fees"] for c in COINS]),
                         trades=np.mean([per[c]["trades"] for c in COINS]), coins_better=better,
                         vs_same_entry=cal(per, "full") - cal(own, "full"),
                         PASS=(e, idea) != ("BASE", "NONE") and cal(per, "h1") > cal(ref, "h1")
                              and cal(per, "h2") > cal(ref, "h2") and better >= 4 and guard))
    T = pd.DataFrame(rows).set_index(["entry", "idea"])
    pd.set_option("display.width", 240)
    print("\n" + T.round(2).to_string())
    T.round(3).to_csv("b_improve_ideas_results.csv")
    return T

if __name__ == "__main__":
    main()
