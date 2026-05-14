"""
Б_hedge — комбинированные сигналы хеджа поверх ИСПРАВЛЕННОГО размера шорта
(short_size = units_spot, т.е. хедж = текущий dollar value спота).

Цель: найти сигнал который **сильнее снижает DD** без потери annual.

Сетка сигналов:
  baseline:
    mom14d, mom7d
  adaptive (LT-фильтр):
    mom7d_lt90d, mom7d_lt180d, mom14d_lt90d, mom14d_lt180d
  early DD trigger:
    dd5, dd8, dd10
  combos (OR):
    mom7d_lt90d | dd5,  mom7d_lt90d | dd8
    mom14d_lt90d | dd5, mom14d_lt90d | dd8
    mom14d | dd5
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import STAKING_YIELD, load_data, buy_and_hold, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_voltgt import simulate_voltgt

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]


def build_signals(close: np.ndarray) -> dict:
    sigs = {}
    s = pd.Series(close)
    mom7  = s.pct_change(7 * 24).fillna(0).values
    mom14 = s.pct_change(14 * 24).fillna(0).values
    mom90 = s.pct_change(90 * 24).fillna(0).values
    mom180 = s.pct_change(180 * 24).fillna(0).values

    running_max = np.maximum.accumulate(close)
    dd = (running_max - close) / running_max

    sigs["mom7d"]           = (mom7 < 0)
    sigs["mom14d"]          = (mom14 < 0)
    sigs["mom7d_lt90d"]     = (mom7 < 0)  & (mom90 < 0)
    sigs["mom7d_lt180d"]    = (mom7 < 0)  & (mom180 < 0)
    sigs["mom14d_lt90d"]    = (mom14 < 0) & (mom90 < 0)
    sigs["mom14d_lt180d"]   = (mom14 < 0) & (mom180 < 0)
    sigs["dd5"]             = (dd > 0.05)
    sigs["dd8"]             = (dd > 0.08)
    sigs["dd10"]            = (dd > 0.10)
    sigs["mom7d_lt90d|dd5"] = sigs["mom7d_lt90d"]  | sigs["dd5"]
    sigs["mom7d_lt90d|dd8"] = sigs["mom7d_lt90d"]  | sigs["dd8"]
    sigs["mom14d_lt90d|dd5"]= sigs["mom14d_lt90d"] | sigs["dd5"]
    sigs["mom14d_lt90d|dd8"]= sigs["mom14d_lt90d"] | sigs["dd8"]
    sigs["mom14d|dd5"]      = sigs["mom14d"]       | sigs["dd5"]
    return sigs


def main():
    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        years = len(df) / HOURS_PER_YEAR
        signals = build_signals(df["close"].values)

        bh_pnl = buy_and_hold(df, staking)
        bh_m   = compute_metrics(bh_pnl)

        for sig_name, sig_arr in signals.items():
            pnl, info = simulate_voltgt(df, staking, target_vol=None, hedge_signal=sig_arr)
            m = compute_metrics(pnl)
            rows.append({
                "coin":         coin,
                "signal":       sig_name,
                "annual":       m["annual_pct"],
                "max_dd":       m["max_dd_pct"],
                "calmar":       m["calmar"],
                "sharpe":       m["sharpe"],
                "pct_hedged":   round(sig_arr.mean() * 100, 1),
                "trades":       info["trades"],
                "funding":      round(info["funding_total"] / TOTAL_CAPITAL / years * 100, 2),
                "hedge_pnl":    round(info["short_realized_pnl"] / TOTAL_CAPITAL / years * 100, 2),
                "fees":         round(-(info["perp_fees_total"] + info["spot_fees_total"]) / TOTAL_CAPITAL / years * 100, 2),
                "bh_annual":    bh_m["annual_pct"],
                "bh_dd":        bh_m["max_dd_pct"],
            })

    df_res = pd.DataFrame(rows)

    print("\n" + "="*120)
    print("Б_hedge — combined сигналы с ИСПРАВЛЕННЫМ размером хеджа (short = units_spot)")
    print("="*120)

    # Per-coin top 5 by Calmar
    for coin in df_res["coin"].unique():
        sub = df_res[df_res["coin"] == coin].sort_values("calmar", ascending=False).head(5)
        bh_a = sub.iloc[0]["bh_annual"]
        bh_d = sub.iloc[0]["bh_dd"]
        print(f"\n{coin}:  buy & hold = {bh_a:.1f}% / DD {bh_d:.1f}% / Calmar {bh_a/bh_d:.2f}")
        cols = ["signal", "annual", "max_dd", "calmar", "sharpe", "pct_hedged",
                "funding", "hedge_pnl", "fees", "trades"]
        print(sub[cols].to_string(index=False))

    # Portfolio (равновзвешенный по 6 коинам)
    print("\n" + "="*120)
    print("Усреднённый портфель (равно-взвешенно по 6 коинам), отсортировано по Calmar")
    print("="*120)
    agg = (df_res.groupby("signal")
                 .agg(annual=("annual", "mean"),
                      max_dd=("max_dd", "mean"),
                      sharpe=("sharpe", "mean"),
                      pct_hedged=("pct_hedged", "mean"),
                      funding=("funding", "mean"),
                      hedge_pnl=("hedge_pnl", "mean"),
                      fees=("fees", "mean"))
                 .round(2))
    agg["calmar"] = (agg["annual"] / agg["max_dd"]).round(2)
    agg = agg.sort_values("calmar", ascending=False)
    cols_order = ["annual", "max_dd", "calmar", "sharpe", "pct_hedged", "funding", "hedge_pnl", "fees"]
    print(agg[cols_order].to_string())

    bh_a = df_res.groupby("coin")["bh_annual"].first().mean()
    bh_d = df_res.groupby("coin")["bh_dd"].first().mean()
    print(f"\nbuy & hold портфель: annual={bh_a:.2f}%, max_dd={bh_d:.2f}%, calmar={bh_a/bh_d:.2f}")

    out = Path(__file__).parent / "backtest_b_combined_results.csv"
    df_res.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
