"""
Эксперимент Б — постоянный спот лонг + селективный шорт перп с regime filter.
Стейкинг включён.

Режимы хеджирования:
  baseline   — хеджируем всегда когда funding высокий
  s1_ma200   — хеджируем только когда цена < MA(200ч) — нисходящий тренд
  s2_mom3d   — хеджируем когда 3-дневный моментум отрицательный
  s2_mom7d   — то же, 7-дневный
  s3_combo   — funding высокий И (цена < MA ИЛИ momentum отрицательный)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from engine import (
    COINS, STAKING_YIELD, load_data, simulate, buy_and_hold, compute_metrics,
    regime_always, regime_below_ma, regime_neg_mom3d, regime_neg_mom7d, regime_combo,
)


def main():
    signals = {
        "baseline":  regime_always,
        "s1_ma200":  regime_below_ma,
        "s2_mom3d":  regime_neg_mom3d,
        "s2_mom7d":  regime_neg_mom7d,
        "s3_combo":  regime_combo,
    }

    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)

        bh_pnl = buy_and_hold(df, staking)
        bh_m   = compute_metrics(bh_pnl)

        for sig_name, sig_fn in signals.items():
            pnl, info = simulate(df, staking, "B", regime_filter=sig_fn)
            m = compute_metrics(pnl)
            rows.append({
                "coin":            coin,
                "signal":          sig_name,
                "annual_pct":      m["annual_pct"],
                "max_dd_pct":      m["max_dd_pct"],
                "calmar":          m["calmar"],
                "sharpe":          m["sharpe"],
                "pct_hedged":      round(info["hours_in_position"] / len(df) * 100, 1),
                "trades":          info["trades"],
                "buy_hold_annual": bh_m["annual_pct"],
                "buy_hold_max_dd": bh_m["max_dd_pct"],
            })

    df_res = pd.DataFrame(rows)

    print("\n" + "="*110)
    print("СРЕДНЕЕ ПО ВСЕМ МОНЕТАМ (Б, со стейкингом)")
    print("="*110)
    avg = (df_res.groupby("signal")
                 .agg(annual=("annual_pct","mean"),
                      max_dd=("max_dd_pct","mean"),
                      calmar=("calmar","mean"),
                      pct_hedged=("pct_hedged","mean"),
                      trades=("trades","mean"))
                 .round(2)
                 .sort_values("calmar", ascending=False))
    bh_avg = df_res.groupby("coin")["buy_hold_annual"].first().mean()
    bh_dd  = df_res.groupby("coin")["buy_hold_max_dd"].first().mean()
    print(avg.to_string())
    print(f"\nbuy & hold (со стейкингом): annual={bh_avg:.2f}%, max_dd={bh_dd:.2f}%")

    print("\n" + "="*110)
    print("ПО МОНЕТАМ: annualized % по сигналам vs buy & hold")
    print("="*110)
    pivot = df_res.pivot(index="coin", columns="signal", values="annual_pct")
    pivot["buy_hold"] = df_res.groupby("coin")["buy_hold_annual"].first()
    cols = ["buy_hold"] + list(signals.keys())
    pivot = pivot[cols].sort_values("baseline", ascending=False)
    print(pivot.round(2).to_string())

    out = Path(__file__).parent / "backtest_b_results.csv"
    df_res.to_csv(out, index=False)
    print(f"\nПолные результаты: {out}")


if __name__ == "__main__":
    main()
