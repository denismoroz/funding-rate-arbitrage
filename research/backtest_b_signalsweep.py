"""
Strategy B — АТАКА НА СИГНАЛ. Улучшить timing-эдж хеджа БЕЗ роста частоты.

Косты фиксированы консервативно (taker 5bps, thr=0.20, lag=1, cash=4%) —
чтобы разница шла от сигнала, а не от допущений об исполнении.
refill_confirm = mom14>0 у всех (изолируем именно hedge-вход).

Сигналы (все причинные, без look-ahead):
  mom14d            — база
  mom30d            — медленнее
  mom21d
  mom14 & mom30     — AND-ансамбль (выше conviction, реже хедж)
  mom14 | mom30     — OR (чаще)
  2of3{7,14,30}     — majority vote
  mom14 & vol_high  — режимный фильтр (хеджим только в турбулентность)
  mom30 & vol_high

Ключевые метрики: hedge_pnl (тайминг-альфа), Calmar, trades/yr, %hedged.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import STAKING_YIELD, load_data, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
THR, SLIP, LAG, CASH = 0.20, 0.0005, 1, 0.04


def cagr(pnl):
    years = len(pnl) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl))
    return (((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100) if end > 0 else -100.0


def build_signals(close: np.ndarray) -> dict:
    s = pd.Series(close)
    def mom(d):
        return (s.pct_change(d * 24).fillna(0).values < 0)
    m7, m14, m21, m30 = mom(7), mom(14), mom(21), mom(30)

    # causal vol-regime: 30d realized vol выше своего 180d среднего
    rets = s.pct_change().fillna(0)
    vol30 = rets.rolling(30 * 24, min_periods=30 * 24).std()
    vol_high = (vol30 > vol30.rolling(180 * 24, min_periods=30 * 24).mean()).fillna(False).values

    twoof3 = (m7.astype(int) + m14.astype(int) + m30.astype(int)) >= 2

    return {
        "mom14d":          m14,
        "mom30d":          m30,
        "mom21d":          m21,
        "mom14&mom30":     m14 & m30,
        "mom14|mom30":     m14 | m30,
        "2of3{7,14,30}":   twoof3,
        "mom14&vol_high":  m14 & vol_high,
        "mom30&vol_high":  m30 & vol_high,
    }


def main():
    data = {}
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        close = df["close"].values
        refill = (pd.Series(close).pct_change(14 * 24).fillna(0).values > 0)  # mom14>0, общий
        data[coin] = (df, STAKING_YIELD.get(coin, 0.0), build_signals(close), refill)

    sig_names = list(next(iter(data.values()))[2].keys())
    rows = []
    for sig in sig_names:
        cg, dd, hp, tr, ph = [], [], [], [], []
        for coin, (df, stk, sigs, refill) in data.items():
            hedge = sigs[sig]
            pnl, info = simulate_constdollar(
                df, stk, hedge, rebal_threshold=THR, risk_free_apr=CASH,
                refill_confirm=refill, signal_lag=LAG, slippage=SLIP,
            )
            m = compute_metrics(pnl)
            years = len(pnl) / HOURS_PER_YEAR
            cg.append(cagr(pnl)); dd.append(m["max_dd_pct"])
            hp.append(info["short_realized_pnl"] / TOTAL_CAPITAL / years * 100)
            tr.append(info["trades"] / years)
            ph.append(info["hours_in_position"] / len(pnl) * 100)
        c, d = np.mean(cg), np.mean(dd)
        rows.append({
            "signal": sig, "CAGR": round(c, 2), "avgDD": round(d, 2),
            "Calmar": round(c / d, 2), "hedge_pnl": round(np.mean(hp), 2),
            "trades_yr": int(np.mean(tr)), "pct_hedged": round(np.mean(ph), 1),
        })

    res = pd.DataFrame(rows).sort_values("Calmar", ascending=False).reset_index(drop=True)
    out = Path(__file__).parent / "backtest_b_signalsweep_results.csv"
    res.to_csv(out, index=False)
    print("=" * 95)
    print("SIGNAL SWEEP — портфель equal-weight (косты фикс: taker 5bps, thr=0.20). Sort by Calmar.")
    print("Цель: выше hedge_pnl/Calmar БЕЗ роста trades_yr.")
    print("=" * 95)
    print(res.to_string(index=False))
    base = res[res.signal == "mom14d"].iloc[0]
    print(f"\nБАЗА mom14d: Calmar {base.Calmar}, hedge_pnl {base.hedge_pnl}, trades/yr {base.trades_yr}")
    print(f"Сохранено: {out}")


if __name__ == "__main__":
    main()
