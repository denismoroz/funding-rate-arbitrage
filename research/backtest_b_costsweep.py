"""
Strategy B — АТАКА НА КОСТЫ. Sweep по трём рычагам снижения churn:

  1. rebal_threshold — насколько редко срезаем излишек спота (шире = меньше churn).
                       'off' = ratchet выключен (чистый hold + hedge).
  2. slippage        — taker 5bps vs maker-ish 2bps.
  3. (min_hold, cooldown) — анти-флаппинг хеджа: держать хедж >= H часов и
                       ждать >= C часов перед повторным входом (режет ~300 сделок/год).

База (honest): thr=0.05, slip=5bps, hold=(0,0). Единый mom14d, lag=1, cash=4%.
Метрики — equal-weight портфель по 6 коинам, CAGR.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from itertools import product
from engine import STAKING_YIELD, load_data, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar, build_mom14d, build_trend_up

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
CASH_APR, SIG_LAG = 0.04, 1

THRESHOLDS = [0.05, 0.10, 0.20, 0.50, 9.99]   # 9.99 == ratchet off
SLIPPAGES  = [0.0005, 0.0002]                 # taker / maker-ish
HOLDS      = [(0, 0), (72, 72), (168, 168)]   # анти-флаппинг


def cagr(pnl):
    years = len(pnl) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl))
    return (((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100) if end > 0 else -100.0


def main():
    # прелоад данных и сигналов
    data = {}
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        close = df["close"].values
        data[coin] = (df, STAKING_YIELD.get(coin, 0.0),
                      build_mom14d(close), build_trend_up(close))

    rows = []
    for thr, slip, (hold, cd) in product(THRESHOLDS, SLIPPAGES, HOLDS):
        cagrs, dds, fees, trades, hpnl = [], [], [], [], []
        for coin, (df, stk, hedge, trend) in data.items():
            pnl, info = simulate_constdollar(
                df, stk, hedge,
                rebal_threshold=thr, risk_free_apr=CASH_APR, refill_confirm=trend,
                signal_lag=SIG_LAG, slippage=slip, min_hold_h=hold, cooldown_h=cd,
            )
            m = compute_metrics(pnl)
            years = len(pnl) / HOURS_PER_YEAR
            cagrs.append(cagr(pnl))
            dds.append(m["max_dd_pct"])
            fees.append(-(info["perp_fees_total"] + info["spot_fees_total"]) / TOTAL_CAPITAL / years * 100)
            trades.append(info["trades"] + info["rebals"])
            hpnl.append(info["short_realized_pnl"] / TOTAL_CAPITAL / years * 100)
        c, d = np.mean(cagrs), np.mean(dds)
        rows.append({
            "thr":    "off" if thr > 1 else f"{thr:.2f}",
            "slip_bps": int(slip * 1e4),
            "hold_h": f"{hold}/{cd}",
            "CAGR":   round(c, 2),
            "avgDD":  round(d, 2),
            "Calmar": round(c / d, 2),
            "fees":   round(np.mean(fees), 2),
            "trades": int(np.mean(trades)),
            "hedge_pnl": round(np.mean(hpnl), 2),
        })

    res = pd.DataFrame(rows).sort_values("Calmar", ascending=False).reset_index(drop=True)
    out = Path(__file__).parent / "backtest_b_costsweep_results.csv"
    res.to_csv(out, index=False)

    base = res[(res.thr == "0.05") & (res.slip_bps == 5) & (res.hold_h == "0/0")].iloc[0]
    print("=" * 100)
    print("COST SWEEP — портфель equal-weight, отсортировано по Calmar")
    print(f"БАЗА (honest): CAGR={base.CAGR}  avgDD={base.avgDD}  Calmar={base.Calmar}  fees={base.fees}  trades/yr~{base.trades}")
    print("=" * 100)
    print(res.to_string(index=False))
    print(f"\nЛучший по Calmar: thr={res.iloc[0].thr} slip={res.iloc[0].slip_bps}bps hold={res.iloc[0].hold_h} "
          f"-> Calmar {res.iloc[0].Calmar} (CAGR {res.iloc[0].CAGR}, fees {res.iloc[0].fees})")
    print(f"Сохранено: {out}")


if __name__ == "__main__":
    main()
