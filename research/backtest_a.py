"""
Эксперимент А — дельта-нейтральный funding harvest.

Два варианта модели:
  A_cycle      — спот и перп открываются/закрываются вместе.
                 4 комиссии за цикл (spot 0.07% × 2 + perp 0.035% × 2).
                 Между циклами — в USDC, нет ценового риска.
  A_spot_keep  — спот куплен один раз, удерживается всегда (cold-wallet модель).
                 Перп открывается/закрывается динамически.
                 Между циклами — полная спот экспозиция + стейкинг.

Параметры стратегии: entry=20% годовых, exit=-5%.
Сравниваем фильтры: min_hold, cooldown, signal_ma.
"""

import pandas as pd
from pathlib import Path
from engine import (
    COINS, STAKING_YIELD, load_data, simulate, compute_metrics,
)


def run_variant(df, staking, strategy, **kwargs):
    pnl_arr, info = simulate(df, staking, strategy, **kwargs)
    m = compute_metrics(pnl_arr)
    return {
        "annual_pct":     m["annual_pct"],
        "max_dd_pct":     m["max_dd_pct"],
        "calmar":         m["calmar"],
        "sharpe":         m["sharpe"],
        "trades":         info["trades"],
        "pct_in_pos":     round(info["hours_in_position"] / len(df) * 100, 1),
    }


def main():
    entry_threshold = 0.20
    exit_threshold  = -0.05
    min_hold_opts   = [0, 24, 72, 168]
    cooldown_opts   = [0, 24, 72, 168]
    signal_w_opts   = [1, 6, 12, 24]

    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            print(f"Пропускаю {coin}")
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        print(f"{coin}: {len(df)} часов")

        for strat in ("A_cycle", "A_spot_keep"):
            # baseline
            base = run_variant(df, staking, strat,
                               entry_threshold=entry_threshold, exit_threshold=exit_threshold)
            rows.append({"coin": coin, "strategy": strat, "filter": "baseline", "param": 0, **base})

            # min_hold
            for h in min_hold_opts[1:]:
                r = run_variant(df, staking, strat,
                                entry_threshold=entry_threshold, exit_threshold=exit_threshold,
                                min_hold=h)
                rows.append({"coin": coin, "strategy": strat, "filter": "min_hold", "param": h, **r})

            # cooldown
            for h in cooldown_opts[1:]:
                r = run_variant(df, staking, strat,
                                entry_threshold=entry_threshold, exit_threshold=exit_threshold,
                                cooldown_hours=h)
                rows.append({"coin": coin, "strategy": strat, "filter": "cooldown", "param": h, **r})

            # signal MA
            for w in signal_w_opts[1:]:
                r = run_variant(df, staking, strat,
                                entry_threshold=entry_threshold, exit_threshold=exit_threshold,
                                signal_window=w)
                rows.append({"coin": coin, "strategy": strat, "filter": "signal_ma", "param": w, **r})

    df_res = pd.DataFrame(rows)

    print("\n" + "="*100)
    print("СРЕДНЕЕ ПО ВСЕМ МОНЕТАМ (entry=20%, exit=-5%)")
    print("="*100)
    avg = (df_res.groupby(["strategy","filter","param"])
                 .agg(avg_annual=("annual_pct","mean"),
                      avg_max_dd=("max_dd_pct","mean"),
                      avg_calmar=("calmar","mean"),
                      avg_trades=("trades","mean"))
                 .round(2))
    print(avg.to_string())

    # По монетам: baseline для обоих вариантов с min_hold=72
    print("\n" + "="*100)
    print("ПО МОНЕТАМ: A_cycle vs A_spot_keep с min_hold=72ч")
    print("="*100)
    sub = df_res[(df_res["filter"] == "min_hold") & (df_res["param"] == 72)]
    pivot = sub.pivot(index="coin", columns="strategy",
                      values=["annual_pct","max_dd_pct","calmar","trades"])
    pivot.columns = [f"{a}_{b}" for a, b in pivot.columns]
    pivot = pivot.sort_values("annual_pct_A_cycle", ascending=False)
    print(pivot.to_string())

    out = Path(__file__).parent / "backtest_a_results.csv"
    df_res.to_csv(out, index=False)
    print(f"\nПолные результаты: {out}")


if __name__ == "__main__":
    main()
