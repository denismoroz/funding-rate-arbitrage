"""
Strategy B — ОРТОГОНАЛЬНЫЕ семейства сигналов (не моментум-вариации).

Косты фикс: taker 5bps, thr=0.20, lag=1, cash=4%. refill=mom14>0.
База = mom14|mom30 (победитель прошлого свипа).

Семейства:
  funding-gate   — хеджить только когда funding>=0 (не платим за шорт)
  funding-add    — доп. хедж когда funding богатый (>20% APR), даже без моментума
  premium/basis  — premium<0 (backwardation = медвежий поток)
  BTC market-gate— системный risk-off: хедж альтов только когда BTC в даунтренде
  Donchian       — price < N-дн. минимум (другой трендовый механизм)
  cross-sectional— хедж только слабейших монет корзины (ранг mom30)
"""
import numpy as np
import pandas as pd
from pathlib import Path
from engine import STAKING_YIELD, load_data, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL, DATA_DIR
from backtest_b_constdollar import simulate_constdollar

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
THR, SLIP, LAG, CASH = 0.20, 0.0005, 1, 0.04


def cagr(pnl):
    years = len(pnl) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl))
    return (((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100) if end > 0 else -100.0


def momc(close, d):  # причинный «mom<0»
    return (pd.Series(close).pct_change(d * 24).fillna(0).values < 0)


def load_premium(coin, index):
    raw = pd.read_csv(DATA_DIR / f"{coin}.csv")
    raw["time"] = pd.to_datetime(raw["time"], format="ISO8601", utc=True).dt.floor("h")
    s = raw.set_index("time")["premium"].sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(index).ffill().fillna(0).values


def main():
    # --- загрузка панели ---
    D = {}
    for coin in COINS:
        df = load_data(coin)
        if not df.empty:
            D[coin] = df
    # BTC daunтренд, выровненный по индексам
    btc = D["BTC"]
    btc_down = pd.Series(momc(btc["close"].values, 30), index=btc.index)
    # cross-sectional панель mom30 (значение, не bool) на union-индексе
    mom30_val = pd.DataFrame({
        c: pd.Series(pd.Series(df["close"].values).pct_change(30 * 24).fillna(0).values, index=df.index)
        for c, df in D.items()
    })
    xsec_rank = mom30_val.rank(axis=1)  # 1 = слабейшая

    def build(coin):
        df = D[coin]; close = df["close"].values; idx = df.index
        m14, m30 = momc(close, 14), momc(close, 30)
        base = m14 | m30
        fund = df["fundingRate"].values
        fund_nonneg = fund >= 0
        fund_rich   = (fund * HOURS_PER_YEAR) > 0.20
        prem_neg    = load_premium(coin, idx) < 0
        btc_d       = btc_down.reindex(idx).ffill().fillna(False).values.astype(bool)
        nlow20      = close < pd.Series(close).rolling(20 * 24, min_periods=20 * 24).min().shift(1).values
        nlow55      = close < pd.Series(close).rolling(55 * 24, min_periods=55 * 24).min().shift(1).values
        xweak       = (xsec_rank[coin].reindex(idx).values <= 3)  # нижняя половина из 6
        return {
            "base mom14|mom30":  base,
            "base & fund>=0":    base & fund_nonneg,
            "base | fund_rich":  base | fund_rich,
            "premium<0":         prem_neg,
            "base | premium<0":  base | prem_neg,
            "btc_down":          btc_d,
            "base & btc_down":   base & btc_d,
            "donchian20":        np.nan_to_num(nlow20).astype(bool),
            "donchian55":        np.nan_to_num(nlow55).astype(bool),
            "xsec_weak":         xweak,
            "base & xsec_weak":  base & xweak,
        }

    sig_names = list(build("ETH").keys())
    rows = []
    for sig in sig_names:
        cg, dd, hp, tr, ph = [], [], [], [], []
        for coin, df in D.items():
            hedge = build(coin)[sig]
            refill = (pd.Series(df["close"].values).pct_change(14 * 24).fillna(0).values > 0)
            pnl, info = simulate_constdollar(df, STAKING_YIELD.get(coin, 0.0), hedge,
                                             rebal_threshold=THR, risk_free_apr=CASH,
                                             refill_confirm=refill, signal_lag=LAG, slippage=SLIP)
            m = compute_metrics(pnl); years = len(pnl) / HOURS_PER_YEAR
            cg.append(cagr(pnl)); dd.append(m["max_dd_pct"])
            hp.append(info["short_realized_pnl"] / TOTAL_CAPITAL / years * 100)
            tr.append(info["trades"] / years); ph.append(info["hours_in_position"] / len(pnl) * 100)
        c, d = np.mean(cg), np.mean(dd)
        rows.append({"signal": sig, "CAGR": round(c, 2), "avgDD": round(d, 2),
                     "Calmar": round(c / d, 2), "hedge_pnl": round(np.mean(hp), 2),
                     "trades_yr": int(np.mean(tr)), "pct_hedged": round(np.mean(ph), 1)})

    res = pd.DataFrame(rows).sort_values("Calmar", ascending=False).reset_index(drop=True)
    out = Path(__file__).parent / "backtest_b_signalsweep2_results.csv"
    res.to_csv(out, index=False)
    print("=" * 98)
    print("ORTHOGONAL SIGNAL SWEEP — портфель equal-weight. Базлайн = mom14|mom30 (Calmar 1.39).")
    print("=" * 98)
    print(res.to_string(index=False))
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
