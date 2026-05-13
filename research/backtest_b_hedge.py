"""
Стратегия Б "stake & hedge": спот держим всегда (стейкинг капает),
шортим перп когда price-сигнал говорит "будет падение".

Funding-условие на вход НЕ требуется (в отличие от старой backtest_b.py).
Funding пока хеджированы — побочный доход (может быть и отриц).

Триггеры хеджа (boolean array per coin):
  ma200       — цена < MA(200ч)
  mom7d       — 7д price return < 0
  mom14d      — 14д price return < 0
  dd10        — drawdown от пика > 10%
  combo       — mom14d OR dd10 (любой "плохой" сигнал)

Цель: уменьшить max drawdown относительно buy & hold, сохранив большую часть upside.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import (
    STAKING_YIELD, load_data, simulate, buy_and_hold, compute_metrics,
    HOURS_PER_YEAR,
)

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]


def build_hedge_signals(df: pd.DataFrame) -> dict:
    close = df["close"].values
    n = len(close)

    ma200  = df["ma200"].values
    mom168 = df["mom_168h"].values  # 7d
    mom336 = pd.Series(close).pct_change(336).fillna(0).values  # 14d

    # Trailing drawdown от running max
    running_max = np.maximum.accumulate(close)
    drawdown = (running_max - close) / running_max

    sig_ma200  = (close < ma200) & ~np.isnan(ma200)
    sig_mom7d  = (mom168 < 0) & ~np.isnan(mom168)
    sig_mom14d = (mom336 < 0)
    sig_dd10   = (drawdown > 0.10)
    sig_combo  = sig_mom14d | sig_dd10

    return {
        "ma200":  sig_ma200,
        "mom7d":  sig_mom7d,
        "mom14d": sig_mom14d,
        "dd10":   sig_dd10,
        "combo":  sig_combo,
    }


def main():
    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            print(f"  {coin}: нет данных, пропускаю")
            continue
        staking = STAKING_YIELD.get(coin, 0.0)

        # Buy & hold benchmark (со стейкингом)
        bh_pnl = buy_and_hold(df, staking)
        bh_m   = compute_metrics(bh_pnl)

        signals = build_hedge_signals(df)
        for sig_name, sig_arr in signals.items():
            pnl, info = simulate(df, staking, "B_hedge", hedge_signal=sig_arr)
            m = compute_metrics(pnl)
            pct_hedged = sig_arr.mean() * 100
            rows.append({
                "coin":         coin,
                "signal":       sig_name,
                "staking":      f"{staking*100:.1f}%",
                "annual_pct":   m["annual_pct"],
                "max_dd_pct":   m["max_dd_pct"],
                "calmar":       m["calmar"],
                "sharpe":       m["sharpe"],
                "pct_hedged":   round(pct_hedged, 1),
                "trades":       info["trades"],
                "bh_annual":    bh_m["annual_pct"],
                "bh_max_dd":    bh_m["max_dd_pct"],
            })

    df_res = pd.DataFrame(rows)

    print("\n" + "="*110)
    print("Стратегия Б_hedge — спот всегда + перп по price-сигналу (без funding-условия)")
    print("="*110)

    print("\n=== По монете × сигналу: annual_pct (со стейкингом) ===")
    pivot = df_res.pivot(index="coin", columns="signal", values="annual_pct")
    pivot["buy_hold"] = df_res.groupby("coin")["bh_annual"].first()
    cols = ["buy_hold", "ma200", "mom7d", "mom14d", "dd10", "combo"]
    pivot = pivot[cols]
    print(pivot.round(2).to_string())

    print("\n=== По монете × сигналу: max_dd_pct ===")
    pivot_dd = df_res.pivot(index="coin", columns="signal", values="max_dd_pct")
    pivot_dd["buy_hold"] = df_res.groupby("coin")["bh_max_dd"].first()
    pivot_dd = pivot_dd[cols]
    print(pivot_dd.round(2).to_string())

    print("\n=== По монете × сигналу: calmar (annual / max_dd) ===")
    pivot_c = df_res.pivot(index="coin", columns="signal", values="calmar")
    pivot_c["buy_hold"] = pivot["buy_hold"] / pivot_dd["buy_hold"]
    pivot_c = pivot_c[cols]
    print(pivot_c.round(2).to_string())

    print("\n=== Среднее по всем монетам ===")
    avg = (df_res.groupby("signal")
                 .agg(annual=("annual_pct", "mean"),
                      max_dd=("max_dd_pct", "mean"),
                      calmar=("calmar", "mean"),
                      pct_hedged=("pct_hedged", "mean"),
                      trades=("trades", "mean"))
                 .round(2)
                 .sort_values("calmar", ascending=False))
    bh_avg_annual = df_res.groupby("coin")["bh_annual"].first().mean()
    bh_avg_dd     = df_res.groupby("coin")["bh_max_dd"].first().mean()
    bh_calmar     = bh_avg_annual / bh_avg_dd if bh_avg_dd > 0 else 0
    print(avg.to_string())
    print(f"\nbuy & hold avg: annual={bh_avg_annual:.2f}%, max_dd={bh_avg_dd:.2f}%, calmar={bh_calmar:.2f}")

    print("\n=== Полные результаты ===")
    print(df_res.to_string(index=False))

    out = Path(__file__).parent / "backtest_b_hedge_results.csv"
    df_res.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
